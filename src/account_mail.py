"""Account mail delivery. Development capture is explicit and never called sent."""
from email.message import EmailMessage
import os
from pathlib import Path
import smtplib
import ssl
from urllib.parse import urlsplit
from uuid import uuid4


def delivery_mode():
    provider = os.environ.get('EMAIL_PROVIDER', '')
    if provider == 'local':
        return 'local' if os.environ.get('APP_ENV') == 'dev' else 'unavailable'
    if provider in ('smtp', 'ses') and os.environ.get('EMAIL_FROM'):
        if provider == 'smtp' and not os.environ.get('SMTP_HOST'):
            return 'unavailable'
        return 'sent'
    return 'unavailable'


def send_account_mail(email, token, purpose):
    mode = delivery_mode()
    if mode == 'unavailable':
        return mode
    origin = os.environ['APP_ORIGIN'].rstrip('/')
    parsed = urlsplit(origin)
    local_http = (os.environ.get('APP_ENV') == 'dev' and parsed.scheme == 'http'
                  and parsed.hostname in ('localhost', '127.0.0.1', '::1'))
    if (parsed.scheme != 'https' and not local_http) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('APP_ORIGIN must be an HTTPS origin for account links')
    reset = purpose == 'reset_password'
    subject = 'Reset your CtrlLoop password' if reset else 'Verify your CtrlLoop email'
    link = origin + '/#' + ('reset' if reset else 'verify') + '=' + token
    body = ('Use this link to ' + ('reset your personal password' if reset else 'verify your email')
            + ':\n\n' + link + '\n\nThis link expires in ' + ('30 minutes' if reset else '24 hours')
            + '. If you did not request this, you can ignore this email.\n')
    message = EmailMessage()
    message['Subject'], message['From'], message['To'] = subject, os.environ.get('EMAIL_FROM', 'local@ctrlloop.invalid'), email
    message.set_content(body)
    if mode == 'local':
        directory = Path(os.environ.get('EMAIL_LOCAL_DIR', '.local/mail'))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (str(uuid4()) + '.eml')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(message.as_string())
        return 'local'
    if os.environ['EMAIL_PROVIDER'] == 'ses':
        import boto3
        from botocore.config import Config
        client = boto3.client('ses', region_name=os.environ.get('AWS_REGION', 'us-west-2'),
                              config=Config(connect_timeout=5, read_timeout=15, retries={'max_attempts': 2}))
        client.send_email(Source=message['From'], Destination={'ToAddresses': [email]},
                          Message={'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                                   'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}}})
    else:
        security = os.environ.get('SMTP_SECURITY', 'starttls')
        if security not in ('starttls', 'ssl'):
            raise ValueError('SMTP_SECURITY must be starttls or ssl')
        factory = smtplib.SMTP_SSL if security == 'ssl' else smtplib.SMTP
        options = {'context': ssl.create_default_context()} if security == 'ssl' else {}
        with factory(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '465' if security == 'ssl' else '587')),
                     timeout=15, **options) as smtp:
            if security == 'starttls':
                smtp.starttls(context=ssl.create_default_context())
            if os.environ.get('SMTP_USERNAME'):
                smtp.login(os.environ['SMTP_USERNAME'], os.environ['SMTP_PASSWORD'])
            smtp.send_message(message)
    return 'sent'
