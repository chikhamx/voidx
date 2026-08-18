package main

import (
	"crypto/sha256"
	"fmt"
	"os"
)

func main() {
	body, err := os.ReadFile("/Users/chikham/workspace/knowledgebase/services/typex-knowledgebase/src/migrations/001_init.sql")
	if err != nil {
		panic(err)
	}
	fmt.Printf("%x\n", sha256.Sum256(body))
}
