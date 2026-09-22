package engine

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Background is a running engine subprocess that outlives the call which
// started it. Run is the wrong shape for the settings UI: that command is a
// server, so it never exits on its own schedule and buffering its output to
// completion would block the tool handler forever.
type Background struct {
	cmd    *exec.Cmd
	stdout io.ReadCloser

	mu       sync.Mutex
	earlyOut strings.Builder
}

// StartBackground launches the engine and returns once the process is
// spawned, without waiting for it to finish.
//
// RunOptions.Timeout is ignored on purpose. A background process must not be
// killed when the tool call returns, so the deadline has to live inside the
// child (the settings server's own --timeout) rather than in a context here.
func StartBackground(opts RunOptions) (*Background, error) {
	if opts.CacheDir == "" {
		return nil, errors.New("engine: CacheDir is required")
	}
	pythonPath, err := resolvePython(opts.PythonPath)
	if err != nil {
		return nil, err
	}
	scriptPath := filepath.Join(opts.CacheDir, "last30days.py")
	if _, err := os.Stat(scriptPath); err != nil {
		return nil, fmt.Errorf("engine: last30days.py not found in cache %s: %w", opts.CacheDir, err)
	}

	args := append([]string{scriptPath}, opts.Args...)
	cmd := exec.Command(pythonPath, args...)
	cmd.Env = buildEnv(opts.CacheDir, opts.ExtraEnv)

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("engine: stdout pipe: %w", err)
	}
	// Fold stderr into stdout so a failure to start (bad config, missing
	// .env) is visible to WaitForLine instead of vanishing.
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("engine: subprocess failed to start: %w", err)
	}

	bg := &Background{cmd: cmd, stdout: stdout}
	return bg, nil
}

// WaitForLine reads the child's output until a line contains match, and
// returns that line. It gives up after timeout, and returns early with
// everything seen so far if the process exits first -- which is the signal
// that the command failed rather than became a server.
//
// The caller keeps ownership of the process either way: a match leaves it
// running (Reap detaches it), and a failure should be followed by Stop.
func (b *Background) WaitForLine(match string, timeout time.Duration) (string, error) {
	type result struct {
		line string
		err  error
	}
	ch := make(chan result, 1)

	go func() {
		scanner := bufio.NewScanner(b.stdout)
		scanner.Buffer(make([]byte, 0, 8192), 1<<20)
		for scanner.Scan() {
			line := scanner.Text()
			b.mu.Lock()
			b.earlyOut.WriteString(line)
			b.earlyOut.WriteString("\n")
			b.mu.Unlock()
			if strings.Contains(line, match) {
				ch <- result{line: line}
				return
			}
		}
		ch <- result{err: errors.New("engine: process produced no matching line")}
	}()

	select {
	case res := <-ch:
		if res.err != nil {
			return "", fmt.Errorf("%w\noutput:\n%s", res.err, b.Output())
		}
		return res.line, nil
	case <-time.After(timeout):
		return "", fmt.Errorf("engine: timed out after %s waiting for output\noutput:\n%s", timeout, b.Output())
	}
}

// Output returns everything read from the child so far.
func (b *Background) Output() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.earlyOut.String()
}

// Reap detaches the process: it keeps running, and a goroutine collects its
// exit status so it does not linger as a zombie in a long-lived MCP server.
func (b *Background) Reap() {
	go func() { _ = b.cmd.Wait() }()
}

// Stop kills the process. Used when startup failed and the child is not
// going to become a usable server.
func (b *Background) Stop() {
	if b.cmd.Process != nil {
		_ = b.cmd.Process.Kill()
	}
	_ = b.cmd.Wait()
}

// Pid reports the child process id, for surfacing in tool output so a user
// can stop the server themselves.
func (b *Background) Pid() int {
	if b.cmd.Process == nil {
		return 0
	}
	return b.cmd.Process.Pid
}
