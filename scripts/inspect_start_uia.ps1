param([string]$OutFile)
# Read-only diagnostic: enumerate HWND metadata and walk only shell UIA roots.
# No Invoke, SetFocus, keyboard, pointer, window activation, or value reads.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ShellWindowProbe {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder b, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder b, int n);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out int v, int n);
}
'@
$shellProcesses = @{}
Get-Process -Name explorer,StartMenuExperienceHost,SearchHost -ErrorAction SilentlyContinue | ForEach-Object { $shellProcesses[$_.Id] = $_.ProcessName }
$windows = [System.Collections.Generic.List[object]]::new()
$callback = [ShellWindowProbe+EnumProc] {
  param($handle, $unused)
  [uint32]$ownerId = 0
  [void][ShellWindowProbe]::GetWindowThreadProcessId($handle, [ref]$ownerId)
  if ($shellProcesses.ContainsKey([int]$ownerId)) {
    $title = [System.Text.StringBuilder]::new(512)
    $class = [System.Text.StringBuilder]::new(256)
    [void][ShellWindowProbe]::GetWindowText($handle, $title, $title.Capacity)
    [void][ShellWindowProbe]::GetClassName($handle, $class, $class.Capacity)
    # Explorer document windows are outside the diagnostic scope.
    if ($shellProcesses[[int]$ownerId] -ne 'explorer' -or $class.ToString() -match '^Shell_(Secondary)?TrayWnd$') {
      [int]$cloaked = 0
      $dwmResult = [ShellWindowProbe]::DwmGetWindowAttribute($handle, 14, [ref]$cloaked, 4)
      $windows.Add([ordered]@{window_id=$handle.ToInt64();pid=$ownerId;process=$shellProcesses[[int]$ownerId];title=$title.ToString();class=$class.ToString();visible=[ShellWindowProbe]::IsWindowVisible($handle);cloaked=$cloaked;dwm_result=$dwmResult})
    }
  }
  return $true
}
$enumerationSucceeded = [ShellWindowProbe]::EnumWindows($callback, [IntPtr]::Zero)
$enumerationError = if ($enumerationSucceeded) { 0 } else { [System.Runtime.InteropServices.Marshal]::GetLastWin32Error() }
$trees = [System.Collections.Generic.List[object]]::new()
foreach ($window in $windows) {
  if (-not $window.visible -or $window.cloaked -ne 0) { continue }
  $rows = [System.Collections.Generic.List[object]]::new()
  try {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$window.window_id)
    $pending = [System.Collections.Generic.Queue[object]]::new()
    $pending.Enqueue(@($root,0))
    while ($pending.Count -gt 0 -and $rows.Count -lt 500) {
      $item = $pending.Dequeue(); $element = $item[0]; $depth = [int]$item[1]
      try {
        $current = $element.Current
        $patterns = @($element.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })
        $rect = $current.BoundingRectangle
        $rows.Add([ordered]@{depth=$depth;name=$current.Name;role=$current.ControlType.ProgrammaticName;automation_id=$current.AutomationId;class=$current.ClassName;pid=$current.ProcessId;native_window_handle=$current.NativeWindowHandle;enabled=$current.IsEnabled;offscreen=$current.IsOffscreen;keyboard_focusable=$current.IsKeyboardFocusable;has_keyboard_focus=$current.HasKeyboardFocus;patterns=$patterns;bounds=@{x=$rect.X;y=$rect.Y;width=$rect.Width;height=$rect.Height}})
        if ($depth -lt 12) {
          $child = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetFirstChild($element)
          while ($null -ne $child) {
            $pending.Enqueue(@($child,($depth+1)))
            $child = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetNextSibling($child)
          }
        }
      } catch { $rows.Add(@{depth=$depth;error=$_.Exception.Message}) }
    }
    $trees.Add(@{window_id=$window.window_id;rows=@($rows.ToArray());pending_nodes=$pending.Count})
  } catch { $trees.Add(@{window_id=$window.window_id;error=$_.Exception.Message}) }
}
$processRows = @($shellProcesses.GetEnumerator() | ForEach-Object { @{pid=$_.Key;name=$_.Value} })
$result = [ordered]@{utc=[DateTime]::UtcNow.ToString('o');shell_processes=$processRows;enumeration_succeeded=$enumerationSucceeded;enumeration_error=$enumerationError;foreground_window_id=[ShellWindowProbe]::GetForegroundWindow().ToInt64();windows=@($windows.ToArray());trees=@($trees.ToArray());limits=@{nodes_per_root=500;depth=12;view='ControlView'}}
$json = $result | ConvertTo-Json -Depth 16
if ($OutFile) {
  $absoluteOutput = [System.IO.Path]::GetFullPath($OutFile)
  [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($absoluteOutput))
  [System.IO.File]::WriteAllText($absoluteOutput,$json)
} else { $json }
