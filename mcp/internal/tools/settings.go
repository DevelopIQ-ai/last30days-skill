package tools

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	mcplib "github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"github.com/mvanhorn/last30days-skill/mcp/internal/engine"
)

const (
	// settingsDefaultIdle is how long the spawned server waits without a
	// request before shutting itself down. The tool cannot stop the process
	// after returning, so the child owns its own lifetime; fifteen minutes is
	// long enough to paste a few keys and short enough that a forgotten
	// server does not hold a port all day.
	settingsDefaultIdle = 900
	settingsMaxIdle     = 86400

	// settingsStartupTimeout bounds how long we wait for the URL line. The
	// engine only has to boot and bind a socket here -- the expensive doctor
	// probe happens later, on the page's first state request -- so this is
	// generous rather than tight.
	settingsStartupTimeout = 90 * time.Second

	settingsURLMarker = "http://127.0.0.1:"
)

func registerSettingsTool(s *server.MCPServer, cfg Config) {
	s.AddTool(
		mcplib.NewTool("settings",
			mcplib.WithDescription(
				"Open the last30days settings page: a local web page listing every source "+
					"with its live status and an inline field for the credential that unlocks "+
					"it, plus every API key the engine reads and where to get one. Use this "+
					"when the user wants to CHANGE configuration (add an API key, turn a "+
					"source on or off, see what is set up); use 'preflight' to inspect "+
					"configuration without changing it. Returns a URL to give the user. The "+
					"page is loopback-only, token-authenticated, and never displays a stored "+
					"key's value.",
			),
			mcplib.WithNumber("idle_timeout", mcplib.Description(
				"Seconds of inactivity before the page shuts itself down (default 900, 0 means never).")),
			mcplib.WithNumber("port", mcplib.Description(
				"Pin the port instead of taking an ephemeral one. Omit unless the user asks.")),
			mcplib.WithReadOnlyHintAnnotation(false),
			// The page can write credentials to the user's .env, but only
			// through an allowlist and only when the user submits the form.
			mcplib.WithDestructiveHintAnnotation(false),
			mcplib.WithOpenWorldHintAnnotation(false),
		),
		makeSettingsHandler(cfg),
	)
}

func makeSettingsHandler(cfg Config) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcplib.CallToolRequest) (*mcplib.CallToolResult, error) {
		args := req.GetArguments()
		idle, err := settingsIntArgument(args, "idle_timeout", settingsDefaultIdle, 0, settingsMaxIdle)
		if err != nil {
			return mcplib.NewToolResultError(err.Error()), nil
		}
		port, err := settingsIntArgument(args, "port", 0, 0, 65535)
		if err != nil {
			return mcplib.NewToolResultError(err.Error()), nil
		}

		src, err := engine.EngineFS()
		if err != nil {
			return mcplib.NewToolResultError(fmt.Sprintf("engine source unavailable: %v", err)), nil
		}
		cacheDir, err := engine.EnsureUserCache(src, cfg.Version)
		if err != nil {
			return mcplib.NewToolResultError(fmt.Sprintf(
				"engine extract failed: %v\nhint: set %s to a writable directory if the default cache location is locked down",
				err, engine.CacheEnvOverride,
			)), nil
		}

		bg, err := engine.StartBackground(engine.RunOptions{
			CacheDir: cacheDir,
			Args:     settingsRunArgs(port, idle),
		})
		if err != nil {
			return mcplib.NewToolResultError(fmt.Sprintf("settings server failed to start: %v", err)), nil
		}

		line, err := bg.WaitForLine(settingsURLMarker, settingsStartupTimeout)
		if err != nil {
			// The child either died or never bound a socket; do not leave it
			// behind either way.
			bg.Stop()
			return mcplib.NewToolResultError(fmt.Sprintf("settings server did not report a URL: %v", err)), nil
		}
		// Hand the process off: it keeps serving, and its exit status is
		// collected so it cannot accumulate as a zombie in a long-lived
		// MCP server.
		bg.Reap()

		return mcplib.NewToolResultText(settingsMessage(strings.TrimSpace(line), idle, bg.Pid())), nil
	}
}

func settingsRunArgs(port, idle int) []string {
	return []string{
		"settings",
		"--no-open",
		"--port", strconv.Itoa(port),
		"--timeout", strconv.Itoa(idle),
	}
}

// settingsMessage is what the model relays. The URL carries a session token,
// so it has to be passed through verbatim rather than described.
func settingsMessage(url string, idle, pid int) string {
	var b strings.Builder
	b.WriteString("last30days settings page is running. Give the user this URL verbatim ")
	b.WriteString("(it carries a one-time session token; without it the page returns 403):\n\n")
	b.WriteString(url)
	b.WriteString("\n\n")
	b.WriteString("Sources tab: every source, its live status, and the credential that unlocks it.\n")
	b.WriteString("API keys tab: every key the engine reads, what it unlocks, and where to get one.\n")
	b.WriteString("Saved keys go to ~/.config/last30days/.env at 0600. Stored values are never shown.\n\n")
	if idle > 0 {
		fmt.Fprintf(&b, "The page shuts down after %d seconds without a request; call this tool again to get a fresh URL.\n", idle)
	} else {
		b.WriteString("The page runs until stopped.\n")
	}
	if pid > 0 {
		fmt.Fprintf(&b, "To stop it sooner: kill %d\n", pid)
	}
	return b.String()
}

// settingsIntArgument reads an optional numeric argument. MCP delivers JSON
// numbers as float64, so a caller sending 900 arrives as 900.0; anything with
// a fractional part is a mistake worth naming rather than truncating.
func settingsIntArgument(args map[string]any, name string, fallback, low, high int) (int, error) {
	raw, ok := args[name]
	if !ok || raw == nil {
		return fallback, nil
	}
	var value int
	switch typed := raw.(type) {
	case float64:
		if typed != float64(int(typed)) {
			return 0, fmt.Errorf("%s must be a whole number, got %v", name, typed)
		}
		value = int(typed)
	case int:
		value = typed
	case string:
		parsed, err := strconv.Atoi(typed)
		if err != nil {
			return 0, fmt.Errorf("%s must be a number, got %q", name, typed)
		}
		value = parsed
	default:
		return 0, fmt.Errorf("%s must be a number", name)
	}
	if value < low || value > high {
		return 0, fmt.Errorf("%s must be between %d and %d, got %d", name, low, high, value)
	}
	return value, nil
}
