package tools

import (
	"strconv"
	"strings"
	"testing"
)

func TestSettingsRunArgsAlwaysPassesNoOpen(t *testing.T) {
	// The MCP server has no display to open a browser on, and on a headless
	// host webbrowser.open can block. --no-open is not optional here.
	args := settingsRunArgs(0, settingsDefaultIdle)
	want := []string{"settings", "--no-open", "--port", "0", "--timeout", "900"}
	if strings.Join(args, "\x00") != strings.Join(want, "\x00") {
		t.Fatalf("args = %#v, want %#v", args, want)
	}
}

func TestSettingsRunArgsCarriesAnIdleDeadline(t *testing.T) {
	// A tool-spawned server cannot be stopped by the tool, so it must always
	// be told when to stop itself; a zero here is the caller's explicit
	// "run forever", never an accidental default.
	for _, idle := range []int{0, 30, settingsMaxIdle} {
		args := settingsRunArgs(0, idle)
		found := false
		for i, a := range args {
			if a == "--timeout" && i+1 < len(args) {
				if args[i+1] != strconv.Itoa(idle) {
					t.Fatalf("idle %d: passed %q", idle, args[i+1])
				}
				found = true
			}
		}
		if !found {
			t.Fatalf("idle %d: --timeout missing from %#v", idle, args)
		}
	}
}

func TestSettingsIntArgumentDefaultsAndValidates(t *testing.T) {
	cases := []struct {
		name    string
		args    map[string]any
		want    int
		wantErr bool
	}{
		{"missing uses fallback", map[string]any{}, 900, false},
		{"nil uses fallback", map[string]any{"idle_timeout": nil}, 900, false},
		// MCP delivers JSON numbers as float64.
		{"float64 accepted", map[string]any{"idle_timeout": float64(300)}, 300, false},
		{"int accepted", map[string]any{"idle_timeout": 300}, 300, false},
		{"numeric string accepted", map[string]any{"idle_timeout": "300"}, 300, false},
		{"zero is valid (run forever)", map[string]any{"idle_timeout": float64(0)}, 0, false},
		{"fractional rejected", map[string]any{"idle_timeout": 1.5}, 0, true},
		{"negative rejected", map[string]any{"idle_timeout": float64(-1)}, 0, true},
		{"over max rejected", map[string]any{"idle_timeout": float64(settingsMaxIdle + 1)}, 0, true},
		{"non-numeric string rejected", map[string]any{"idle_timeout": "soon"}, 0, true},
		{"bool rejected", map[string]any{"idle_timeout": true}, 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := settingsIntArgument(tc.args, "idle_timeout", 900, 0, settingsMaxIdle)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr = %v", err, tc.wantErr)
			}
			if !tc.wantErr && got != tc.want {
				t.Fatalf("got %d, want %d", got, tc.want)
			}
		})
	}
}

func TestSettingsPortArgumentIsRangeChecked(t *testing.T) {
	if _, err := settingsIntArgument(map[string]any{"port": float64(70000)}, "port", 0, 0, 65535); err == nil {
		t.Fatal("port 70000 should be rejected")
	}
	got, err := settingsIntArgument(map[string]any{"port": float64(8787)}, "port", 0, 0, 65535)
	if err != nil || got != 8787 {
		t.Fatalf("got %d, err %v", got, err)
	}
}

func TestSettingsMessageCarriesTheURLVerbatim(t *testing.T) {
	// The URL holds the session token. If the model paraphrases it the page
	// returns 403, so the message has to present it as copy-exactly text.
	url := "http://127.0.0.1:54321/?t=Zm9vYmFyYmF6"
	msg := settingsMessage(url, 900, 4242)
	if !strings.Contains(msg, url) {
		t.Fatalf("message dropped the URL:\n%s", msg)
	}
	if !strings.Contains(msg, "verbatim") {
		t.Fatalf("message should tell the model to pass the URL through verbatim:\n%s", msg)
	}
	if !strings.Contains(msg, "kill 4242") {
		t.Fatalf("message should name the pid so the user can stop it:\n%s", msg)
	}
	if !strings.Contains(msg, "900 seconds") {
		t.Fatalf("message should state the idle deadline:\n%s", msg)
	}
}

func TestSettingsMessageDescribesAnUnboundedServer(t *testing.T) {
	msg := settingsMessage("http://127.0.0.1:1/?t=x", 0, 0)
	if strings.Contains(msg, "shuts down after") {
		t.Fatalf("idle 0 must not claim a deadline:\n%s", msg)
	}
	if !strings.Contains(msg, "runs until stopped") {
		t.Fatalf("idle 0 should say the server runs until stopped:\n%s", msg)
	}
}

func TestSettingsMessageNeverSuggestsKeysAreVisible(t *testing.T) {
	// A model reading this should not tell a user it can read their stored
	// keys back to them -- the page deliberately cannot.
	msg := settingsMessage("http://127.0.0.1:1/?t=x", 900, 1)
	if !strings.Contains(msg, "never shown") {
		t.Fatalf("message should state stored values are never shown:\n%s", msg)
	}
}
