//go:build tools

// Package tools pins the Go programs this repo's checks run. The import keeps
// the requirement direct: a `tool` directive records it as `// indirect`, and
// Dependabot bumps only direct requirements by default.
package tools

import _ "github.com/jbeda/mdreflow/cmd/mdreflow"
