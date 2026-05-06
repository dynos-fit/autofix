// Fixture nested-module Go package for task-20260417-006 GoAdapter tests.
// The presence of a sibling go.mod means this file's nearest-ancestor
// go.mod is this directory's go.mod (NOT the repo-root go.mod), which
// exercises the innermost-go.mod grouping algorithm in GoAdapter.
package util

// Add returns the sum of two integers.
func Add(a, b int) int {
	return a + b
}
