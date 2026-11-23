"""Switch the account of the Xbox app (and so Game Bar) by working its own menu, the way a screen reader would.

The Xbox app has no link or API to sign out; its own sign-in link (msxbox://signIn/) left it stuck on "Launching". So
BRPW starts the Xbox app normally and uses Windows UI Automation (the accessibility interface) to click
"Profile & settings" > "Sign out", then "Sign in" on the screen that follows, which opens Microsoft's own account
picker. When the account's email is known (saved at sign-in, see msauth.email_from), that account is selected in the
picker and Continue pressed; otherwise the user picks. Any password is typed by the user; BRPW never sees it.

Clicks are real mouse clicks, made only when the point under the mouse belongs to the Xbox window (so nothing in
another window is ever clicked), and the mouse is put back afterwards. Names and IDs were read from the real Xbox app
(version 2612, English) on 2026-11-23; Dutch names are accepted too.
"""
import os
import subprocess
from typing import Callable, Optional

# Results (the last line the script prints)
ALREADY = "ALREADY"              # already signed in as the wanted account
PICKER = "PICKER"                # signed out (if needed) and Sign in pressed: Microsoft's account picker is open
SIGN_IN_SCREEN = "SIGN_IN_SCREEN"  # signed out, but Sign in couldn't be pressed: the user clicks it
NO_WINDOW = "NO_WINDOW"          # the Xbox app didn't show its window (stuck on "Launching"?)
NOT_READY = "NOT_READY"          # window there, but neither the profile button nor the sign-in screen appeared
BLOCKED = "BLOCKED"              # something covers the Xbox window: no click was made
NO_SIGN_OUT = "NO_SIGN_OUT"      # the profile menu had no Sign out item
PICKED = "PICKED"                # the account with the saved email was chosen in the picker and Continue pressed
NOT_LISTED = "NOT_LISTED"        # the picker is open but has no account with that email: the user picks

SCRIPT = r'''
$ErrorActionPreference = "Stop"
$target = $env:BRPW_TARGET
$email = $env:BRPW_EMAIL
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class BrpwW {
  [StructLayout(LayoutKind.Sequential)] public struct P { public int X; public int Y; }
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(P p);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint f);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out P p);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  public delegate bool CB(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(CB cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder s, int n);
  public static System.Collections.Generic.List<IntPtr> Frames() {
    var l = new System.Collections.Generic.List<IntPtr>();
    EnumWindows((h, x) => { if (IsWindowVisible(h)) { var c = new System.Text.StringBuilder(64); GetClassName(h, c, 64);
      if (c.ToString() == "ApplicationFrameWindow") l.Add(h); } return true; }, IntPtr.Zero);
    return l; }
}
"@
$A = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$True_ = [System.Windows.Automation.Condition]::TrueCondition
function ById($root, $id) { $root.FindFirst($TS::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::AutomationIdProperty, $id))) }
function XboxWindow { $A::RootElement.FindAll($TS::Children, $True_) |
    Where-Object { $_.Current.ClassName -eq 'ApplicationFrameWindow' -and $_.Current.Name -eq 'XBOX' } | Select-Object -First 1 }
function Front($w) { $h = [IntPtr]$w.Current.NativeWindowHandle; [BrpwW]::ShowWindow($h, 9) | Out-Null
    [BrpwW]::BringWindowToTop($h) | Out-Null; Start-Sleep -Milliseconds 300 }
function ClickOn($w, $el) {
    $h = [IntPtr]$w.Current.NativeWindowHandle
    if ($w -is [IntPtr]) { $h = $w }
    $r = $el.Current.BoundingRectangle
    if ($r.IsEmpty -or $r.Width -le 0) { return $false }
    $p = New-Object BrpwW+P; $p.X = [int]($r.X + $r.Width / 2); $p.Y = [int]($r.Y + $r.Height / 2)
    if ([BrpwW]::GetAncestor([BrpwW]::WindowFromPoint($p), 2) -ne $h) { return $false }   # not the Xbox window: no click
    $old = New-Object BrpwW+P; [BrpwW]::GetCursorPos([ref]$old) | Out-Null
    [BrpwW]::SetCursorPos($p.X, $p.Y) | Out-Null
    [BrpwW]::mouse_event(2, 0, 0, 0, [UIntPtr]::Zero); [BrpwW]::mouse_event(4, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 100
    [BrpwW]::SetCursorPos($old.X, $old.Y) | Out-Null
    return $true
}
function Press($w, $el) {                      # a checked click; Invoke only as a fallback (it can wait for the picker)
    Front $w
    if (ClickOn $w $el) { return $true }
    $p = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); return $true }
    return $false
}

# 1. the Xbox window with either the profile button (signed in) or the sign-in screen
$w = $null; $profile = $null; $signin = $null
for ($i = 0; $i -lt 90; $i++) {
    $w = XboxWindow
    if ($w) {
        $profile = ById $w 'ProfileSettingsButton_MainButton'
        $signin = ById $w 'AccountSignInScreen-SignInButton'
        if ($profile -or $signin) { break }
    }
    Start-Sleep -Milliseconds 500
}
if (-not $w) { "NO_WINDOW"; exit }
if (-not ($profile -or $signin)) { "NOT_READY"; exit }

# 2. signed in: already the wanted account? otherwise Profile & settings > Sign out
if ($profile) {
    $name = $profile.Current.Name
    "SIGNED_IN_AS: $name"
    if ($target -and $name.ToLower().Contains(("- " + $target + " ").ToLower())) { "ALREADY"; exit }
    Front $w
    if (-not (ClickOn $w $profile)) { "BLOCKED"; exit }
    $so = $null
    for ($i = 0; $i -lt 30 -and -not $so; $i++) {
        Start-Sleep -Milliseconds 150
        foreach ($n in @('Sign out', 'Afmelden', 'Uitloggen')) {
            $so = $w.FindFirst($TS::Descendants, (New-Object System.Windows.Automation.AndCondition(
                (New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, $n)),
                (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::MenuItem)))))
            if ($so) { break }
        }
    }
    if (-not $so) { "NO_SIGN_OUT"; exit }
    if (-not (ClickOn $w $so)) { "BLOCKED"; exit }
    "SIGNED_OUT"
    for ($i = 0; $i -lt 60 -and -not $signin; $i++) { Start-Sleep -Milliseconds 500; $signin = ById $w 'AccountSignInScreen-SignInButton' }
    if (-not $signin) { "SIGN_IN_SCREEN"; exit }
}

# 3. the sign-in screen: press Sign in, which opens Microsoft's account picker
Start-Sleep -Milliseconds 500
if (-not (Press $w $signin)) { "SIGN_IN_SCREEN"; exit }
"PICKER"
if (-not $email) { exit }

# 4. the picker ("Sign in" window with ExistingAccounts): select the account with the saved email, press Continue
$pw = $null; $list = $null
for ($i = 0; $i -lt 40 -and -not $list; $i++) {
    Start-Sleep -Milliseconds 500
    foreach ($h in [BrpwW]::Frames()) {
        $e = $A::FromHandle($h)
        $l = ById $e 'ExistingAccounts'
        if ($l) { $pw = $h; $list = $l; break }
    }
}
if (-not $list) { exit }
$item = $null
foreach ($it in $list.FindAll($TS::Children, $True_)) {
    $first = ($it.Current.Name -split "`n")[0].Trim()
    if ($first.ToLower() -eq $email.Trim().ToLower()) { $item = $it; break }
}
if (-not $item) { "NOT_LISTED"; exit }
$sel = $null
if ($item.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$sel)) { $sel.Select() }
elseif (-not (ClickOn $pw $item)) { "NOT_LISTED"; exit }
Start-Sleep -Milliseconds 400
$cont = ById ($A::FromHandle($pw)) 'ContinueButton'
if (-not $cont) { exit }
[BrpwW]::BringWindowToTop($pw) | Out-Null; Start-Sleep -Milliseconds 300
if (ClickOn $pw $cont) { "PICKED"; exit }
$p = $null
if ($cont.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); "PICKED" }
'''


def switch_account(target: str, email: str = "", run: Optional[Callable] = None, timeout: int = 120) -> str:
    """Run the script; returns one of the result constants (or "ERROR: ..."). Blocks: call it off the UI thread.
    With the account's email, that account is chosen in Microsoft's picker too (no password is ever typed)."""
    env = dict(os.environ, BRPW_TARGET=(target or "").strip(), BRPW_EMAIL=(email or "").strip())
    try:
        res = (run or subprocess.run)(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", SCRIPT],
            env=env, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return f"ERROR: {e}"
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return "ERROR: " + ((res.stderr or "").strip().splitlines() or ["no answer"])[-1][:200]
    return lines[-1]
