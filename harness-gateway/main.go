package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type Index struct {
	Generated   string                   `json:"generated"`
	RecordCount int                      `json:"record_count"`
	Records     []map[string]interface{} `json:"records"`
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8765", "HTTP listen address")
	indexPath := flag.String("index", defaultIndexPath(), "harness_index.json path")
	ollamaHost := flag.String("ollama", defaultOllamaHost(), "local Ollama host for supplemental model calls")
	allowRemote := flag.Bool("allow-remote", false, "permit binding a non-loopback address; the gateway has no auth and exposes harness search/stats plus an Ollama proxy")
	flag.Parse()

	if !*allowRemote && !isLoopbackAddr(*addr) {
		log.Fatalf("refusing to bind %q: not a loopback address.\n"+
			"The gateway has no authentication and exposes harness search/stats and an Ollama embed/model proxy.\n"+
			"Bind 127.0.0.1 (default), or pass -allow-remote only if you understand and accept the exposure.", *addr)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]interface{}{"ok": true, "service": "harness-gateway", "ollama_host": *ollamaHost})
	})
	mux.HandleFunc("/harness/stats", func(w http.ResponseWriter, r *http.Request) {
		index, err := loadIndex(*indexPath)
		if err != nil {
			http.Error(w, err.Error(), http.StatusServiceUnavailable)
			return
		}
		writeJSON(w, stats(index, *indexPath))
	})
	mux.HandleFunc("/harness/search", func(w http.ResponseWriter, r *http.Request) {
		index, err := loadIndex(*indexPath)
		if err != nil {
			http.Error(w, err.Error(), http.StatusServiceUnavailable)
			return
		}
		query := strings.TrimSpace(r.URL.Query().Get("q"))
		harness := strings.TrimSpace(r.URL.Query().Get("harness"))
		kind := strings.TrimSpace(r.URL.Query().Get("kind"))
		writeJSON(w, search(index, query, harness, kind, 10))
	})
	mux.HandleFunc("/supplemental/embed", func(w http.ResponseWriter, r *http.Request) {
		proxyOllamaJSON(w, r, *ollamaHost, "/api/embed")
	})
	mux.HandleFunc("/supplemental/models", func(w http.ResponseWriter, r *http.Request) {
		proxyOllamaJSON(w, r, *ollamaHost, "/api/tags")
	})

	log.Printf("harness-gateway listening on http://%s", *addr)
	log.Fatal(http.ListenAndServe(*addr, mux))
}

// isLoopbackAddr reports whether addr binds only the local machine.
// An empty host (e.g. ":8765") binds all interfaces and is treated as remote.
func isLoopbackAddr(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		host = addr
	}
	host = strings.TrimSpace(host)
	if host == "" {
		return false
	}
	if strings.EqualFold(host, "localhost") {
		return true
	}
	if ip := net.ParseIP(host); ip != nil {
		return ip.IsLoopback()
	}
	return false
}

// defaultIndexPath resolves the harness index path with dual-brand support.
// Precedence: ALGO_CLI_INDEX_PATH > OLLAMA_CLI_INDEX_PATH > ~/.algo_cli/harness_index.json (if exists) > ~/.ollama_cli/harness_index.json
func defaultIndexPath() string {
	if configured := os.Getenv("ALGO_CLI_INDEX_PATH"); configured != "" {
		return configured
	}
	if configured := os.Getenv("OLLAMA_CLI_INDEX_PATH"); configured != "" {
		return configured
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "harness_index.json"
	}
	newPath := filepath.Join(home, ".algo_cli", "harness_index.json")
	if _, err := os.Stat(newPath); err == nil {
		return newPath
	}
	return filepath.Join(home, ".ollama_cli", "harness_index.json")
}

func defaultOllamaHost() string {
	if configured := os.Getenv("ALGO_CLI_HOST"); configured != "" {
		return configured
	}
	if configured := os.Getenv("OLLAMA_HOST"); configured != "" {
		return configured
	}
	return "http://localhost:11434"
}

func loadIndex(path string) (Index, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Index{}, err
	}
	var index Index
	if err := json.Unmarshal(data, &index); err != nil {
		return Index{}, err
	}
	return index, nil
}

func stats(index Index, path string) map[string]interface{} {
	counts := map[string]int{}
	for _, record := range index.Records {
		key := stringValue(record, "harness") + ":" + stringValue(record, "kind")
		counts[key]++
	}
	return map[string]interface{}{
		"index":        path,
		"generated":    index.Generated,
		"record_count": index.RecordCount,
		"counts":       counts,
	}
}

func search(index Index, query string, harness string, kind string, limit int) []map[string]interface{} {
	terms := strings.Fields(strings.ToLower(query))
	type scoredRecord struct {
		score  int
		record map[string]interface{}
	}
	scored := []scoredRecord{}
	for _, record := range index.Records {
		if harness != "" && stringValue(record, "harness") != harness {
			continue
		}
		if kind != "" && stringValue(record, "kind") != kind {
			continue
		}
		score := scoreRecord(record, terms)
		if score > 0 {
			scored = append(scored, scoredRecord{score: score, record: record})
		}
	}
	sort.Slice(scored, func(i, j int) bool {
		return scored[i].score > scored[j].score
	})
	results := []map[string]interface{}{}
	for i, item := range scored {
		if i >= limit {
			break
		}
		results = append(results, item.record)
	}
	return results
}

func scoreRecord(record map[string]interface{}, terms []string) int {
	haystack := strings.ToLower(stringValue(record, "search_text"))
	if haystack == "" {
		haystack = strings.ToLower(strings.Join([]string{
			stringValue(record, "id"),
			stringValue(record, "harness"),
			stringValue(record, "kind"),
			stringValue(record, "title"),
			stringValue(record, "description"),
			stringValue(record, "relative_path"),
			stringValue(record, "summary"),
		}, " "))
	}
	score := 0
	title := strings.ToLower(stringValue(record, "title"))
	rel := strings.ToLower(stringValue(record, "relative_path"))
	for _, term := range terms {
		if strings.Contains(haystack, term) {
			score++
		}
		if strings.Contains(title, term) {
			score += 3
		}
		if strings.Contains(rel, term) {
			score += 2
		}
	}
	return score
}

func stringValue(record map[string]interface{}, key string) string {
	value, ok := record[key]
	if !ok || value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

func writeJSON(w http.ResponseWriter, payload interface{}) {
	w.Header().Set("Content-Type", "application/json")
	encoder := json.NewEncoder(w)
	encoder.SetIndent("", "  ")
	_ = encoder.Encode(payload)
}

func proxyOllamaJSON(w http.ResponseWriter, r *http.Request, ollamaHost string, apiPath string) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var body io.Reader
	if r.Body != nil {
		data, err := io.ReadAll(io.LimitReader(r.Body, 10<<20))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		body = bytes.NewReader(data)
	}
	target := strings.TrimRight(ollamaHost, "/") + apiPath
	req, err := http.NewRequest(r.Method, target, body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}
