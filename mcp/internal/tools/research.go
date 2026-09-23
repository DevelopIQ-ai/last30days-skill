// Package tools owns the MCP tool surface for last30days.
package tools

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	mcplib "github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"github.com/mvanhorn/last30days-skill/mcp/internal/engine"
)

// Config carries the version string used to namespace the per-user cache.
// main passes its ldflags-stamped Version here.
type Config struct {
	Version string
}

// Register adds every tool this server exposes to s. The caller supplies a
// Config so test harnesses can pin a version without touching globals.
func Register(s *server.MCPServer, cfg Config) {
	registerPreflightTool(s, cfg)
	registerSettingsTool(s, cfg)
	s.AddTool(
		mcplib.NewTool("research",
			mcplib.WithDescription(
				"Research what people are actually saying about any topic in the last 30 days. "+
					"Aggregates Reddit, X, YouTube, Hacker News, Polymarket, GitHub, and the web, "+
					"scored by upvotes, likes, transcripts, and real-money prediction-market odds. "+
					"Returns the engine's compact output for the model to synthesize.\n\n"+
					"YOU ARE THE PLANNER. Always pass `plan`. The engine has no model of "+
					"its own: without a plan it searches the raw topic string once, with no "+
					"decomposition and no disambiguation, which is how a search for a company "+
					"returns unrelated results that merely share its name. Writing the plan "+
					"costs you one step and no API key -- you are the LLM the engine lacks.",
			),
			mcplib.WithString("topic", mcplib.Required(), mcplib.Description("The subject to research (a person, company, product, event, or general topic).")),
			mcplib.WithString("emit", mcplib.Description("Output shape: 'compact' (default) for inline synthesis or 'html' to save a shareable brief alongside the response.")),
			mcplib.WithBoolean("save", mcplib.Description("Persist the synthesis as a markdown report under ~/Documents/Last30Days/ (or LAST30DAYS_MEMORY_DIR if set).")),
			mcplib.WithString("plan", mcplib.Description(
				"JSON query plan you author. Strongly recommended -- omitting it drops the "+
					"engine to a single literal-string search. Shape: "+
					`{"intent":"entity|concept|breaking_news","freshness_mode":"strict_recent|evergreen_ok",`+
					`"cluster_mode":"story|none","subqueries":[{"label":"primary",`+
					`"search_query":"<what to search>","ranking_query":"<the question results are ranked against>",`+
					`"sources":["reddit","x","hackernews","youtube","github","grounding"],"weight":1.0}]}. `+
					"Write 2-4 subqueries that attack different angles, and disambiguate in "+
					"search_query when the topic name is ambiguous.")),
			mcplib.WithReadOnlyHintAnnotation(false),
			mcplib.WithDestructiveHintAnnotation(false),
			mcplib.WithOpenWorldHintAnnotation(true),
		),
		makeResearchHandler(cfg),
	)
}

func makeResearchHandler(cfg Config) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcplib.CallToolRequest) (*mcplib.CallToolResult, error) {
		args := req.GetArguments()
		topic, err := requireString(args, "topic")
		if err != nil {
			return mcplib.NewToolResultError(err.Error()), nil
		}

		emit, err := emitArgument(args)
		if err != nil {
			return mcplib.NewToolResultError(err.Error()), nil
		}

		save, err := boolArgument(args, "save")
		if err != nil {
			return mcplib.NewToolResultError(err.Error()), nil
		}

		plan, err := planArgument(args)
		if err != nil {
			return mcplib.NewToolResultError(err.Error()), nil
		}
		// The engine reads --plan from a file path transparently. Going
		// through a temp file avoids handing a JSON blob to the shell, the
		// same reason SKILL.md tells the model to use mktemp: an apostrophe
		// in a ranking_query otherwise breaks the invocation.
		var planPath string
		if plan != "" {
			planPath, err = writeTempPlan(plan)
			if err != nil {
				return mcplib.NewToolResultError(fmt.Sprintf("could not stage plan: %v", err)), nil
			}
			defer os.Remove(planPath)
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

		runArgs := researchRunArgs(topic, emit, save, planPath)

		res, runErr := engine.Run(ctx, engine.RunOptions{
			CacheDir: cacheDir,
			Args:     runArgs,
			// An MCP host is an agent host: it has its own web search and is
			// expected to do the general-web lane itself, which is the same
			// declaration SKILL.md makes on agent hosts. Without it the engine
			// would both run its worse keyless floor and fail the web
			// capability check.
			ExtraEnv: []string{"LAST30DAYS_NATIVE_SEARCH=1"},
		})
		if runErr != nil {
			// Exit 3 is the capability gate declining the work, not a crash.
			// Its stderr is a set of instructions addressed to this model, so
			// surface it as such -- "subprocess exited with code 3" reads like
			// a broken install and invites a retry that will fail identically.
			if res != nil && res.ExitCode == engineRefusedExitCode {
				return mcplib.NewToolResultError(formatRefusal(res)), nil
			}
			return mcplib.NewToolResultError(formatRunError(runErr, res)), nil
		}
		return mcplib.NewToolResultText(string(res.Stdout)), nil
	}
}

func researchRunArgs(topic, emit string, save bool, planPath string) []string {
	// --agent-rerank is unconditional here: the caller of this tool is by
	// definition a model that can judge relevance while synthesizing, and the
	// engine only acts on the flag when it has no reranking model of its own.
	// Passing it never degrades a key-configured install, and without it the
	// capability gate would refuse every keyless MCP run.
	runArgs := []string{topic, "--emit=" + emit, "--no-browser-cookies", "--agent-rerank"}
	if save {
		runArgs = append(runArgs, "--save-dir", mcpSaveDir())
	}
	if planPath != "" {
		runArgs = append(runArgs, "--plan", planPath)
	}
	return runArgs
}

// planArgument accepts the plan as a JSON object or as a JSON string, since
// models emit both. It is validated here so a malformed plan is a tool error
// naming the problem, rather than a silent drop to the engine's one-query
// fallback -- which is the failure this whole argument exists to prevent.
func planArgument(args map[string]any) (string, error) {
	raw, ok := args["plan"]
	if !ok || raw == nil {
		return "", nil
	}
	var encoded []byte
	switch typed := raw.(type) {
	case string:
		if strings.TrimSpace(typed) == "" {
			return "", nil
		}
		encoded = []byte(typed)
	case map[string]any:
		var err error
		encoded, err = json.Marshal(typed)
		if err != nil {
			return "", fmt.Errorf("plan could not be encoded: %w", err)
		}
	default:
		return "", errors.New("plan must be a JSON object or a JSON string")
	}

	var probe struct {
		Subqueries []struct {
			SearchQuery string `json:"search_query"`
		} `json:"subqueries"`
	}
	if err := json.Unmarshal(encoded, &probe); err != nil {
		return "", fmt.Errorf("plan is not valid JSON: %w", err)
	}
	if len(probe.Subqueries) == 0 {
		return "", errors.New("plan needs at least one entry in subqueries")
	}
	for i, sq := range probe.Subqueries {
		if strings.TrimSpace(sq.SearchQuery) == "" {
			return "", fmt.Errorf("plan subquery %d has an empty search_query", i+1)
		}
	}
	return string(encoded), nil
}

func writeTempPlan(plan string) (string, error) {
	f, err := os.CreateTemp("", "last30days-plan-*.json")
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err := f.WriteString(plan); err != nil {
		os.Remove(f.Name())
		return "", err
	}
	return f.Name(), nil
}

func mcpSaveDir() string {
	saveDir := os.Getenv("LAST30DAYS_MEMORY_DIR")
	if saveDir == "" {
		return "~/Documents/Last30Days"
	}
	return saveDir
}

func requireString(args map[string]any, name string) (string, error) {
	raw, ok := args[name]
	if !ok {
		return "", fmt.Errorf("%s is required", name)
	}
	value, ok := raw.(string)
	if !ok || strings.TrimSpace(value) == "" {
		return "", fmt.Errorf("%s must be a non-empty string", name)
	}
	return value, nil
}

func emitArgument(args map[string]any) (string, error) {
	raw, ok := args["emit"]
	if !ok {
		return "compact", nil
	}
	value, ok := raw.(string)
	if !ok {
		return "", errors.New("emit must be a string")
	}
	switch value {
	case "":
		return "compact", nil
	case "compact", "html":
		return value, nil
	default:
		return "", fmt.Errorf("emit must be 'compact' or 'html', got %q", value)
	}
}

func boolArgument(args map[string]any, name string) (bool, error) {
	raw, ok := args[name]
	if !ok {
		return false, nil
	}
	value, ok := raw.(bool)
	if !ok {
		return false, fmt.Errorf("%s must be a boolean", name)
	}
	return value, nil
}

// formatRunError flattens engine.Run's distinct error shapes into a single
// user-facing message that includes the relevant stderr context.
// engineRefusedExitCode is what last30days.py returns when the capability
// gate declines a run. Distinct from a crash so a caller can tell "you did
// not give me what I need" from "something broke".
const engineRefusedExitCode = 3

// formatRefusal leads with what the model must do differently. The engine's
// own block already names each gap, its effect, and both routes, so this adds
// only the framing the exit code alone does not carry.
func formatRefusal(res *engine.RunResult) string {
	var msg strings.Builder
	msg.WriteString("The research engine declined this run: it was not given what it needs ")
	msg.WriteString("to produce a trustworthy result. This is not a failure to retry as-is.\n\n")
	msg.WriteString("Pass a `plan` argument to this tool (you are the planner) and rerun. ")
	msg.WriteString("If a web-search capability is also listed below, either configure one of ")
	msg.WriteString("the named keys or do the general-web searching yourself.\n\n")
	msg.Write(res.Stderr)
	return msg.String()
}

func formatRunError(runErr error, res *engine.RunResult) string {
	var msg strings.Builder
	msg.WriteString(runErr.Error())
	if res != nil && len(res.Stderr) > 0 {
		msg.WriteString("\nengine stderr:\n")
		msg.Write(res.Stderr)
	}
	return msg.String()
}
