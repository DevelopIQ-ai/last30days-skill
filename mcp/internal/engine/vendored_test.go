package engine

import (
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"testing"
)

// syncedEngine reports whether scripts/sync-engine.sh has populated the embed
// directory. vendored/ is gitignored, so a clean checkout embeds only the
// .gitkeep anchor and there is nothing to assert about. Every build that
// matters -- local `go build` after sync, and CI before packaging -- runs the
// script first, so this skip narrows the tests below to exactly the builds
// where the contract is real.
func syncedEngine(t *testing.T) fs.FS {
	t.Helper()
	src, err := EngineFS()
	if err != nil {
		t.Fatalf("EngineFS: %v", err)
	}
	if _, err := fs.Stat(src, "last30days.py"); err != nil {
		t.Skip("engine not vendored; run mcp/scripts/sync-engine.sh")
	}
	return src
}

// The engine reports its own version by walking up from lib/render.py for
// .claude-plugin/plugin.json, then SKILL.md. In a normal install both sit
// above scripts/. The extracted MCP cache is scripts/ content only, so
// without the manifest every report rendered through the MCP server
// announced itself as "last30days v?".
func TestVendoredEngineCarriesThePluginManifest(t *testing.T) {
	src := syncedEngine(t)

	raw, err := fs.ReadFile(src, ".claude-plugin/plugin.json")
	if err != nil {
		t.Fatalf("plugin manifest missing from the embedded engine: %v\n"+
			"without it the engine cannot resolve its version and reports 'v?'", err)
	}
	var manifest struct {
		Version string `json:"version"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatalf("vendored manifest is not valid JSON: %v", err)
	}
	if manifest.Version == "" {
		t.Fatal("vendored manifest carries no version")
	}
}

// //go:embed all:vendored is what keeps the dot-directory. A plain
// //go:embed would drop it silently, and the only symptom would be the
// version banner going back to "?".
func TestEmbedPreservesDotDirectories(t *testing.T) {
	src := syncedEngine(t)

	found := false
	err := fs.WalkDir(src, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && d.Name() == ".claude-plugin" {
			found = true
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk: %v", err)
	}
	if !found {
		t.Fatal("no .claude-plugin directory in the embedded FS; " +
			"the //go:embed directive likely lost its all: prefix")
	}
}

// The extractor has to carry the dot-directory through to disk as well --
// embedding it is only half the path to a working version lookup.
func TestEnsureUserCacheExtractsTheManifest(t *testing.T) {
	src := syncedEngine(t)

	dir := t.TempDir()
	t.Setenv(CacheEnvOverride, dir)
	cacheDir, err := EnsureUserCache(src, "test-version")
	if err != nil {
		t.Fatalf("EnsureUserCache: %v", err)
	}
	for _, name := range []string{"last30days.py", ".claude-plugin/plugin.json"} {
		if _, err := os.Stat(filepath.Join(cacheDir, name)); err != nil {
			t.Fatalf("%s not extracted into the cache: %v", name, err)
		}
	}
}
