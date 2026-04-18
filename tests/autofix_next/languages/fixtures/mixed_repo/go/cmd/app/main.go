// Fixture Go main package for task-20260417-006 GoAdapter tests.
package main

import "fmt"

func main() {
	fmt.Println(greet("world"))
}

func greet(name string) string {
	return "hello, " + name
}
