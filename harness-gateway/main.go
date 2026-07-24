package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	maxIndexBytes         = 64 << 20
	maxProxyRequestBytes  = 10 << 20
	maxProxyResponseBytes = 20 << 20
	maxSearchQueryBytes   = 4 << 10
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
	allowRemote := flag.Bool("allow-remote", false, "deprecated; unauthenticated remote binding is disabled")
	flag.Parse()

	if !isLoopbackAddr(*addr) {
		log.Fatalf("refusing non-loopback gateway exposure: bind an explicit loopback address")
	}
	if *allowRemote {
		log.Printf("-allow-remote is deprecated and ignored; gateway remains loopback-only")
	}
	validatedOllama, err := validateOllamaHost(*ollamaHost)
	if err != nil {
		log.Fatalf("invalid local Ollama endpoint: %v", err)
	}
	mux := newGatewayMux(*indexPath, validatedOllama, loopbackHTTPClient())

	log.Printf("harness-gateway listening on http://%s", *addr)
	server := &http.Server{
		Addr:              *addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
	log.Fatal(server.ListenAndServe())
}

func newGatewayMux(indexPath string, ollamaHost string, client *http.Client) http.Handler {
	client = noRedirectHTTPClient(client)
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		writeJSON(w, map[string]interface{}{"ok": true, "service": "harness-gateway"})
	})
	mux.HandleFunc("/harness/stats", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		index, err := loadIndex(indexPath)
		if err != nil {
			writeGatewayError(w, http.StatusServiceUnavailable, "index_unavailable")
			return
		}
		writeJSON(w, stats(index))
	})
	mux.HandleFunc("/harness/search", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		query := strings.TrimSpace(r.URL.Query().Get("q"))
		harness := strings.TrimSpace(r.URL.Query().Get("harness"))
		kind := strings.TrimSpace(r.URL.Query().Get("kind"))
		if !boundedQueryValue(query, maxSearchQueryBytes) ||
			!boundedQueryValue(harness, 128) || !boundedQueryValue(kind, 128) {
			writeGatewayError(w, http.StatusBadRequest, "query_bounds")
			return
		}
		index, err := loadIndex(indexPath)
		if err != nil {
			writeGatewayError(w, http.StatusServiceUnavailable, "index_unavailable")
			return
		}
		writeJSON(w, search(index, query, harness, kind, 10))
	})
	mux.HandleFunc("/supplemental/embed", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodPost) {
			return
		}
		proxyOllamaJSON(w, r, client, ollamaHost, "/api/embed")
	})
	mux.HandleFunc("/supplemental/models", func(w http.ResponseWriter, r *http.Request) {
		if !requireMethod(w, r, http.MethodGet) {
			return
		}
		proxyOllamaJSON(w, r, client, ollamaHost, "/api/tags")
	})
	return mux
}

func noRedirectHTTPClient(client *http.Client) *http.Client {
	if client == nil {
		client = &http.Client{}
	}
	clone := *client
	clone.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return &clone
}

// isLoopbackAddr reports whether addr binds only the local machine.
// An empty host (e.g. ":8765") binds all interfaces and is treated as remote.
func isLoopbackAddr(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
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

func validateOllamaHost(raw string) (string, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || parsed.Host == "" {
		return "", fmt.Errorf("endpoint_syntax")
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("endpoint_scheme")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" ||
		(parsed.Path != "" && parsed.Path != "/") {
		return "", fmt.Errorf("endpoint_components")
	}
	host := parsed.Hostname()
	if !strings.EqualFold(host, "localhost") {
		ip := net.ParseIP(host)
		if ip == nil || !ip.IsLoopback() {
			return "", fmt.Errorf("endpoint_not_loopback")
		}
	}
	if port := parsed.Port(); port != "" {
		parsedPort, err := strconv.Atoi(port)
		if err != nil || parsedPort < 1 || parsedPort > 65_535 {
			return "", fmt.Errorf("endpoint_port")
		}
	}
	return parsed.Scheme + "://" + parsed.Host, nil
}

func loopbackHTTPClient() *http.Client {
	dialer := &net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy:                 nil,
		DisableCompression:    true,
		ForceAttemptHTTP2:     false,
		MaxIdleConns:          4,
		MaxIdleConnsPerHost:   2,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   5 * time.Second,
		ResponseHeaderTimeout: 10 * time.Second,
	}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		host, port, err := net.SplitHostPort(address)
		if err != nil {
			return nil, fmt.Errorf("upstream_address")
		}
		addresses, err := net.DefaultResolver.LookupIP(ctx, "ip", host)
		if err != nil || len(addresses) == 0 {
			return nil, fmt.Errorf("upstream_resolution")
		}
		for _, candidate := range addresses {
			if !candidate.IsLoopback() {
				return nil, fmt.Errorf("upstream_not_loopback")
			}
		}
		return dialer.DialContext(ctx, network, net.JoinHostPort(addresses[0].String(), port))
	}
	return &http.Client{
		Transport: transport,
		Timeout:   30 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
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
	return "http://127.0.0.1:11434"
}

func loadIndex(path string) (Index, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
		return Index{}, fmt.Errorf("index_file")
	}
	if info.Size() < 0 || info.Size() > maxIndexBytes {
		return Index{}, fmt.Errorf("index_size")
	}
	file, err := os.Open(path)
	if err != nil {
		return Index{}, err
	}
	defer file.Close()
	openedInfo, err := file.Stat()
	if err != nil || !openedInfo.Mode().IsRegular() || !os.SameFile(info, openedInfo) {
		return Index{}, fmt.Errorf("index_changed")
	}
	data, err := io.ReadAll(io.LimitReader(file, maxIndexBytes+1))
	if err != nil || len(data) > maxIndexBytes {
		return Index{}, fmt.Errorf("index_read")
	}
	var index Index
	if err := json.Unmarshal(data, &index); err != nil {
		return Index{}, err
	}
	if index.Generated == "" || index.RecordCount != len(index.Records) || index.RecordCount > 100_000 {
		return Index{}, fmt.Errorf("index_contract")
	}
	return index, nil
}

func stats(index Index) map[string]interface{} {
	counts := map[string]int{}
	for _, record := range index.Records {
		key := stringValue(record, "harness") + ":" + stringValue(record, "kind")
		counts[key]++
	}
	return map[string]interface{}{
		"generated":    index.Generated,
		"record_count": index.RecordCount,
		"counts":       counts,
		"index_loaded": true,
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
	sort.SliceStable(scored, func(i, j int) bool {
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
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	encoder := json.NewEncoder(w)
	encoder.SetIndent("", "  ")
	_ = encoder.Encode(payload)
}

func proxyOllamaJSON(
	w http.ResponseWriter,
	r *http.Request,
	client *http.Client,
	ollamaHost string,
	apiPath string,
) {
	var body io.Reader
	if r.Method == http.MethodPost {
		mediaType, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || mediaType != "application/json" {
			writeGatewayError(w, http.StatusUnsupportedMediaType, "content_type")
			return
		}
		data, err := io.ReadAll(io.LimitReader(r.Body, maxProxyRequestBytes+1))
		if err != nil {
			writeGatewayError(w, http.StatusBadRequest, "request_read")
			return
		}
		if len(data) > maxProxyRequestBytes {
			writeGatewayError(w, http.StatusRequestEntityTooLarge, "request_too_large")
			return
		}
		body = bytes.NewReader(data)
	}
	target := strings.TrimRight(ollamaHost, "/") + apiPath
	req, err := http.NewRequestWithContext(r.Context(), r.Method, target, body)
	if err != nil {
		writeGatewayError(w, http.StatusInternalServerError, "upstream_request")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		writeGatewayError(w, http.StatusBadGateway, "upstream_unavailable")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		writeGatewayError(w, http.StatusBadGateway, "upstream_redirect")
		return
	}
	mediaType, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || mediaType != "application/json" {
		writeGatewayError(w, http.StatusBadGateway, "upstream_content_type")
		return
	}
	payload, err := io.ReadAll(io.LimitReader(resp.Body, maxProxyResponseBytes+1))
	if err != nil || len(payload) > maxProxyResponseBytes {
		writeGatewayError(w, http.StatusBadGateway, "upstream_response_bounds")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(payload)
}

func requireMethod(w http.ResponseWriter, r *http.Request, method string) bool {
	if r.Method == method {
		return true
	}
	w.Header().Set("Allow", method)
	writeGatewayError(w, http.StatusMethodNotAllowed, "method_not_allowed")
	return false
}

func boundedQueryValue(value string, maximumBytes int) bool {
	if len(value) > maximumBytes {
		return false
	}
	for _, character := range value {
		if character < 0x20 || character == 0x7f {
			return false
		}
	}
	return true
}

func writeGatewayError(w http.ResponseWriter, status int, reason string) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]interface{}{
		"ok":          false,
		"reason_code": reason,
	})
}
