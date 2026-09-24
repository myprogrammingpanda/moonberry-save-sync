# Shows a small "create shortcuts" window (Desktop / Start Menu
# checkboxes) and creates whichever shortcuts were ticked. Used by
# run.bat once after first-time setup (-FirstRun) and by
# create-shortcuts.bat any time after that.
#
# Shortcuts point at run.bat (minimized, so its brief console doesn't
# flash) rather than straight at pythonw.exe, so they keep working
# after a Python reinstall/upgrade and still get run.bat's setup checks.
#
# Exit code: 0 = done (shortcuts created, or the user skipped), 1 = error.

param(
    [switch]$FirstRun
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
# Without this, Windows bitmap-stretches the window on scaled (125%+)
# displays and the text comes out blurry.
try {
    Add-Type -Namespace Moonberry -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
    [Moonberry.Dpi]::SetProcessDPIAware() | Out-Null
} catch { }
[System.Windows.Forms.Application]::EnableVisualStyles()

$AppName = 'Moonberry Save-Sync'
$AppDir  = Split-Path -Parent $PSScriptRoot
$RunBat  = Join-Path $AppDir 'run.bat'
$Icon    = Join-Path $AppDir 'assets\moonberry.ico'

$DesktopLnk   = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
$StartMenuLnk = Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"

# --- Dialog -------------------------------------------------------------

$form = New-Object System.Windows.Forms.Form
$form.Text = $AppName
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.AutoSize = $true
$form.AutoSizeMode = 'GrowAndShrink'
$form.Padding = New-Object System.Windows.Forms.Padding(12)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)
if (Test-Path $Icon) { $form.Icon = New-Object System.Drawing.Icon($Icon) }

$layout = New-Object System.Windows.Forms.FlowLayoutPanel
$layout.FlowDirection = 'TopDown'
$layout.AutoSize = $true
$layout.AutoSizeMode = 'GrowAndShrink'
$layout.WrapContents = $false
$form.Controls.Add($layout)

$heading = New-Object System.Windows.Forms.Label
$heading.AutoSize = $true
$heading.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
if ($FirstRun) { $heading.Text = 'Setup complete!' } else { $heading.Text = 'Create shortcuts' }
$layout.Controls.Add($heading)

$intro = New-Object System.Windows.Forms.Label
$intro.AutoSize = $true
$intro.Text = "Create shortcuts to launch ${AppName}?"
$intro.Margin = New-Object System.Windows.Forms.Padding(3, 6, 3, 10)
$layout.Controls.Add($intro)

$chkDesktop = New-Object System.Windows.Forms.CheckBox
$chkDesktop.AutoSize = $true
$chkDesktop.Text = 'Desktop shortcut'
$chkDesktop.Checked = $true
$layout.Controls.Add($chkDesktop)

$chkStart = New-Object System.Windows.Forms.CheckBox
$chkStart.AutoSize = $true
$chkStart.Text = 'Start Menu shortcut'
$chkStart.Checked = $true
$layout.Controls.Add($chkStart)

if ($FirstRun) {
    $note = New-Object System.Windows.Forms.Label
    $note.AutoSize = $true
    $note.ForeColor = [System.Drawing.SystemColors]::GrayText
    $note.Text = 'You can make these later with create-shortcuts.bat.'
    $note.Margin = New-Object System.Windows.Forms.Padding(3, 10, 3, 0)
    $layout.Controls.Add($note)
}

$buttons = New-Object System.Windows.Forms.FlowLayoutPanel
$buttons.FlowDirection = 'RightToLeft'
$buttons.AutoSize = $true
$buttons.AutoSizeMode = 'GrowAndShrink'
$buttons.Dock = 'Fill'
$buttons.Margin = New-Object System.Windows.Forms.Padding(0, 14, 0, 0)
$layout.Controls.Add($buttons)

$btnSkip = New-Object System.Windows.Forms.Button
$btnSkip.Text = 'Skip'
$btnSkip.DialogResult = 'Cancel'
$buttons.Controls.Add($btnSkip)

$btnCreate = New-Object System.Windows.Forms.Button
$btnCreate.Text = 'Create'
$btnCreate.DialogResult = 'OK'
$buttons.Controls.Add($btnCreate)

$form.AcceptButton = $btnCreate
$form.CancelButton = $btnSkip

# Only allow "Create" when at least one box is ticked.
$sync = { $btnCreate.Enabled = $chkDesktop.Checked -or $chkStart.Checked }
$chkDesktop.Add_CheckedChanged($sync)
$chkStart.Add_CheckedChanged($sync)

if ($form.ShowDialog() -ne 'OK') { exit 0 }

# --- Create -------------------------------------------------------------

function New-AppShortcut([string]$Path) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $RunBat
    $lnk.WorkingDirectory = $AppDir
    $lnk.WindowStyle = 7   # minimized: hides run.bat's brief console
    $lnk.Description = $AppName
    if (Test-Path $Icon) { $lnk.IconLocation = "$Icon,0" }
    $lnk.Save()
}

try {
    if ($chkDesktop.Checked) { New-AppShortcut $DesktopLnk }
    if ($chkStart.Checked)   { New-AppShortcut $StartMenuLnk }
} catch {
    [System.Windows.Forms.MessageBox]::Show(
        "Couldn't create the shortcut:`n`n$($_.Exception.Message)",
        $AppName, 'OK', 'Warning') | Out-Null
    exit 1
}

exit 0
