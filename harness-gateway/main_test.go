package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
)

func writeFixtureIndex(t *testing.T, records []map[string]interface{}) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "harness_index.json")
	payload := Index{Generated: "1800000000", RecordCount: len(records), Records: records}
	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func decodeResponse(t *testing.T, response *httptest.ResponseRecorder) map[string]interface{} {
	t.Helper()
	var payload map[string]interface{}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatalf("invalid JSON response: %v: %q", err, response.Body.String())
	}
	return payload
}

func TestLoopbackListenAddressRequiresExplicitHostAndPort(t *testing.T) {
	accepted := []string{"127.0.0.1:8765", "localhost:1", "[::1]:65535"}
	for _, address := range accepted {
		if !isLoopbackAddr(address) {
			t.Errorf("expected loopback address: %s", address)
		}
	}
	rejected := []string{"", ":8765", "0.0.0.0:8765", "192.168.1.2:8765", "example.com:8765", "127.0.0.1"}
	for _, address := range rejected {
		if isLoopbackAddr(address) {
			t.Errorf("unexpected accepted address: %s", address)
		}
	}
}

func TestOllamaEndpointIsCredentialFreePathlessAndLoopbackOnly(t *testing.T) {
	accepted := map[string]string{
		"http://127.0.0.1:11434/": "http://127.0.0.1:11434",
		"https://localhost:443":   "https://localhost:443",
		"http://[::1]:11434":      "http://[::1]:11434",
	}
	for raw, expected := range accepted {
		actual, err := validateOllamaHost(raw)
		if err != nil || actual != expected {
			t.Errorf("validateOllamaHost(%q) = %q, %v", raw, actual, err)
		}
	}
	rejected := []string{
		"http://10.0.0.1:11434",
		"http://example.com:11434",
		"http://user:secret@127.0.0.1:11434",
		"file:///tmp/socket",
		"http://127.0.0.1:11434/api/tags",
		"http://127.0.0.1:11434?q=secret",
		"http://127.0.0.1:bad",
	}
	for _, raw := range rejected {
		if _, err := validateOllamaHost(raw); err == nil {
			t.Errorf("unexpected accepted endpoint: %s", raw)
		}
	}
}

func TestIndexLoaderBoundsContractAndSymlinks(t *testing.T) {
	valid := writeFixtureIndex(t, []map[string]interface{}{{"id": "one"}})
	index, err := loadIndex(valid)
	if err != nil || index.RecordCount != 1 {
		t.Fatalf("valid index rejected: %#v, %v", index, err)
	}

	mismatch := filepath.Join(t.TempDir(), "mismatch.json")
	if err := os.WriteFile(mismatch, []byte(`{"generated":"1","record_count":2,"records":[]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadIndex(mismatch); err == nil {
		t.Fatal("record-count mismatch accepted")
	}

	large := filepath.Join(t.TempDir(), "large.json")
	file, err := os.Create(large)
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Truncate(maxIndexBytes + 1); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := loadIndex(large); err == nil {
		t.Fatal("oversized index accepted")
	}

	if runtime.GOOS != "windows" {
		link := filepath.Join(t.TempDir(), "linked.json")
		if err := os.Symlink(valid, link); err != nil {
			t.Fatal(err)
		}
		if _, err := loadIndex(link); err == nil {
			t.Fatal("symlink index accepted")
		}
	}
}

func TestGatewayMethodsQueriesSearchAndPrivacy(t *testing.T) {
	records := []map[string]interface{}{
		{
			"id": "algo-cli:memory:one", "harness": "algo-cli", "kind": "memory",
			"title": "Control safety", "relative_path": "safe.md", "search_text": "control safety",
		},
		{
			"id": "other:skill:two", "harness": "other", "kind": "skill",
			"title": "Other", "relative_path": "other.md", "search_text": "control other",
		},
	}
	indexPath := writeFixtureIndex(t, records)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"models":[]}`)
	}))
	defer upstream.Close()
	mux := newGatewayMux(indexPath, upstream.URL, upstream.Client())

	health := httptest.NewRecorder()
	mux.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if health.Code != http.StatusOK || strings.Contains(health.Body.String(), "ollama") {
		t.Fatalf("unsafe health response: %d %s", health.Code, health.Body.String())
	}

	method := httptest.NewRecorder()
	mux.ServeHTTP(method, httptest.NewRequest(http.MethodPost, "/healthz", nil))
	if method.Code != http.StatusMethodNotAllowed || method.Header().Get("Allow") != http.MethodGet {
		t.Fatalf("method gate failed: %d", method.Code)
	}

	statsResponse := httptest.NewRecorder()
	mux.ServeHTTP(statsResponse, httptest.NewRequest(http.MethodGet, "/harness/stats", nil))
	statsPayload := decodeResponse(t, statsResponse)
	if _, leaked := statsPayload["index"]; leaked || strings.Contains(statsResponse.Body.String(), indexPath) {
		t.Fatalf("stats leaked local path: %s", statsResponse.Body.String())
	}

	searchResponse := httptest.NewRecorder()
	mux.ServeHTTP(
		searchResponse,
		httptest.NewRequest(http.MethodGet, "/harness/search?q=control&harness=algo-cli", nil),
	)
	var searchPayload []map[string]interface{}
	if err := json.Unmarshal(searchResponse.Body.Bytes(), &searchPayload); err != nil {
		t.Fatal(err)
	}
	if len(searchPayload) != 1 || searchPayload[0]["id"] != "algo-cli:memory:one" {
		t.Fatalf("unexpected search results: %#v", searchPayload)
	}

	bounded := httptest.NewRecorder()
	request := httptest.NewRequest(
		http.MethodGet,
		"/harness/search?q="+strings.Repeat("a", maxSearchQueryBytes+1),
		nil,
	)
	mux.ServeHTTP(bounded, request)
	if bounded.Code != http.StatusBadRequest {
		t.Fatalf("oversized query accepted: %d", bounded.Code)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

type zeroReader struct{ remaining int64 }

func (reader *zeroReader) Read(destination []byte) (int, error) {
	if reader.remaining <= 0 {
		return 0, io.EOF
	}
	count := len(destination)
	if int64(count) > reader.remaining {
		count = int(reader.remaining)
	}
	for index := 0; index < count; index++ {
		destination[index] = '0'
	}
	reader.remaining -= int64(count)
	return count, nil
}

func TestProxyRejectsTruncationRedirectsAndOversizedResponses(t *testing.T) {
	var calls atomic.Int32
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		calls.Add(1)
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"ok":true}`)),
			Request:    request,
		}, nil
	})}
	mux := newGatewayMux("missing", "http://127.0.0.1:11434", client)

	tooLarge := httptest.NewRecorder()
	request := httptest.NewRequest(
		http.MethodPost,
		"/supplemental/embed",
		io.LimitReader(&zeroReader{remaining: maxProxyRequestBytes + 1}, maxProxyRequestBytes+1),
	)
	request.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(tooLarge, request)
	if tooLarge.Code != http.StatusRequestEntityTooLarge || calls.Load() != 0 {
		t.Fatalf("oversized request reached upstream: status=%d calls=%d", tooLarge.Code, calls.Load())
	}

	redirectClient := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusFound,
			Header: http.Header{
				"Content-Type": []string{"application/json"},
				"Location":     []string{"http://127.0.0.1:1/private"},
			},
			Body:    io.NopCloser(strings.NewReader(`{}`)),
			Request: request,
		}, nil
	})}
	redirectMux := newGatewayMux("missing", "http://127.0.0.1:11434", redirectClient)
	redirect := httptest.NewRecorder()
	redirectMux.ServeHTTP(redirect, httptest.NewRequest(http.MethodGet, "/supplemental/models", nil))
	if redirect.Code != http.StatusBadGateway || !strings.Contains(redirect.Body.String(), "upstream_redirect") {
		t.Fatalf("redirect accepted: %d %s", redirect.Code, redirect.Body.String())
	}

	largeClient := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(&zeroReader{remaining: maxProxyResponseBytes + 1}),
			Request:    request,
		}, nil
	})}
	largeMux := newGatewayMux("missing", "http://127.0.0.1:11434", largeClient)
	large := httptest.NewRecorder()
	largeMux.ServeHTTP(large, httptest.NewRequest(http.MethodGet, "/supplemental/models", nil))
	if large.Code != http.StatusBadGateway || !strings.Contains(large.Body.String(), "upstream_response_bounds") {
		t.Fatalf("oversized response accepted: %d", large.Code)
	}
}

func TestProxyForwardsOnlyFixedJSONRequestAndSecurityHeaders(t *testing.T) {
	var upstreamPath string
	var upstreamAuthorization string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamPath = r.URL.Path
		upstreamAuthorization = r.Header.Get("Authorization")
		body, err := io.ReadAll(r.Body)
		if err != nil || !bytes.Equal(body, []byte(`{"input":"hello"}`)) {
			t.Errorf("unexpected body: %q, %v", body, err)
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Set-Cookie", "secret=value")
		_, _ = io.WriteString(w, `{"embedding":[1]}`)
	}))
	defer upstream.Close()
	mux := newGatewayMux("missing", upstream.URL, upstream.Client())
	response := httptest.NewRecorder()
	request := httptest.NewRequest(
		http.MethodPost,
		"/supplemental/embed",
		strings.NewReader(`{"input":"hello"}`),
	)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer must-not-forward")
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusOK || upstreamPath != "/api/embed" {
		t.Fatalf("proxy failed: status=%d path=%s", response.Code, upstreamPath)
	}
	if upstreamAuthorization != "" || response.Header().Get("Set-Cookie") != "" {
		t.Fatal("proxy forwarded sensitive headers")
	}
	if response.Header().Get("Cache-Control") != "no-store" ||
		response.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatal("security headers missing")
	}
}
