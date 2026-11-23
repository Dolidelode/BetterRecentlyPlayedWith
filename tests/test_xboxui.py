import subprocess

from brpw import xboxui


class Done:
    def __init__(self, out="", err=""):
        self.stdout, self.stderr = out, err


def test_the_last_line_is_the_result_and_the_target_goes_in_by_environment():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return Done("SIGNED_IN_AS: Profile & settings - Salty Seadog signed in\nSIGNED_OUT\nPICKER\n")
    assert xboxui.switch_account(" Captain Kraken ", run=run) == xboxui.PICKER
    assert seen["env"]["BRPW_TARGET"] == "Captain Kraken"              # not pasted into the script text
    assert seen["cmd"][0] == "powershell.exe" and seen["cmd"][-1] == xboxui.SCRIPT


def test_errors_are_reported_not_raised():
    assert xboxui.switch_account("x", run=lambda *a, **k: Done("", "Add-Type failed\nboom")) == "ERROR: boom"

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("powershell", 1)
    assert xboxui.switch_account("x", run=timeout).startswith("ERROR:")


def test_the_script_only_clicks_inside_the_xbox_window():
    s = xboxui.SCRIPT
    assert "WindowFromPoint" in s and "-ne $h) { return $false }" in s      # the point must belong to the Xbox window
    assert "AccountSignInScreen-SignInButton" in s and "ProfileSettingsButton_MainButton" in s
    assert "msxbox://signIn" not in s                                     # the link that hung the Xbox app


def test_the_email_goes_in_by_environment_and_picking_is_reported():
    seen = {}

    def run(cmd, **kw):
        seen["env"] = kw["env"]
        return Done("SIGNED_OUT\nPICKER\nPICKED\n")
    assert xboxui.switch_account("Salty Seadog", " pirate@example.com ", run=run) == xboxui.PICKED
    assert seen["env"]["BRPW_EMAIL"] == "pirate@example.com"
    assert "ExistingAccounts" in xboxui.SCRIPT and "ContinueButton" in xboxui.SCRIPT
