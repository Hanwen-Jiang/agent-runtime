package crawler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"html"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

type SearchResult struct {
	Title   string   `json:"title"`
	URL     string   `json:"url"`
	Snippet string   `json:"snippet"`
	Domain  string   `json:"domain"`
	Score   *float64 `json:"score,omitempty"`
}

var (
	reComment    = regexp.MustCompile(`(?is)<!--.*?-->`)
	reBoiler     = regexp.MustCompile(`(?is)<(script|style|noscript|svg|nav|footer|aside)\b[^>]*>.*?</(?:script|style|noscript|svg|nav|footer|aside)>`)
	reTitle      = regexp.MustCompile(`(?is)<title\b[^>]*>(.*?)</title>`)
	reBlockBreak = regexp.MustCompile(`(?i)</?(p|div|section|article|li|tr|h[1-6]|pre|blockquote|br|hr)\b[^>]*>`)
	reAnyTag     = regexp.MustCompile(`(?s)<[^>]+>`)
	reSpaces     = regexp.MustCompile(`[ \t]+`)
	reDDGLink    = regexp.MustCompile(`(?is)<a\b[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>`)
	reDDGSnip    = regexp.MustCompile(`(?is)<(?:a|div|span)\b[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|span)>`)
)

// ExtractReadable strips boilerplate markup and returns page title plus clean paragraph text.
func ExtractReadable(rawHTML string) (string, string) {
	if !strings.Contains(rawHTML, "<") || !strings.Contains(rawHTML, ">") {
		return "", strings.TrimSpace(rawHTML)
	}
	title := ""
	if m := reTitle.FindStringSubmatch(rawHTML); len(m) == 2 {
		title = normalizeInline(reAnyTag.ReplaceAllString(m[1], " "))
	}
	cleaned := reComment.ReplaceAllString(rawHTML, " ")
	cleaned = reBoiler.ReplaceAllString(cleaned, " ")
	cleaned = reBlockBreak.ReplaceAllString(cleaned, "\n")
	cleaned = reAnyTag.ReplaceAllString(cleaned, " ")
	cleaned = html.UnescapeString(cleaned)

	var out []string
	blank := false
	for _, line := range strings.Split(cleaned, "\n") {
		line = strings.TrimSpace(reSpaces.ReplaceAllString(line, " "))
		if line == "" {
			if len(out) > 0 && !blank {
				out = append(out, "")
			}
			blank = true
		} else {
			out = append(out, line)
			blank = false
		}
	}
	text := strings.TrimSpace(strings.Join(out, "\n"))
	if text == "" {
		text = strings.TrimSpace(rawHTML)
	}
	return title, text
}

func normalizeInline(s string) string {
	return strings.TrimSpace(reSpaces.ReplaceAllString(html.UnescapeString(s), " "))
}

func decodeDDGHref(href string) string {
	raw := strings.TrimSpace(html.UnescapeString(href))
	if strings.HasPrefix(raw, "//") {
		raw = "https:" + raw
	}
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	if uddg := u.Query().Get("uddg"); uddg != "" {
		return uddg
	}
	if u.Scheme == "http" || u.Scheme == "https" {
		return raw
	}
	return ""
}

// ParseDDGHTML parses DuckDuckGo HTML / Lite search pages into SearchResult entries.
func ParseDDGHTML(rawHTML string) []SearchResult {
	links := reDDGLink.FindAllStringSubmatchIndex(rawHTML, -1)
	snips := reDDGSnip.FindAllStringSubmatch(rawHTML, -1)
	results := make([]SearchResult, 0, len(links))
	for i, loc := range links {
		href := decodeDDGHref(rawHTML[loc[2]:loc[3]])
		if href == "" {
			continue
		}
		title := normalizeInline(reAnyTag.ReplaceAllString(rawHTML[loc[4]:loc[5]], " "))
		snippet := ""
		if i < len(snips) && len(snips[i]) == 2 {
			snippet = normalizeInline(reAnyTag.ReplaceAllString(snips[i][1], " "))
		}
		if len(snippet) > 600 {
			snippet = snippet[:600]
		}
		domain := ""
		if u, err := url.Parse(href); err == nil {
			domain = strings.ToLower(u.Hostname())
		}
		results = append(results, SearchResult{
			Title:   title,
			URL:     href,
			Snippet: snippet,
			Domain:  domain,
		})
	}
	return results
}

// SearchConfig defines upstream web search providers supported by go-crawler-gateway.
type SearchConfig struct {
	Provider    string // auto | ddg_lite | searxng | tavily
	UpstreamURL string
	APIKey      string
	HeadlessURL string
	HeadlessTok string
	Timeout     time.Duration
}

type SearchBackend struct {
	cfg    SearchConfig
	client *http.Client
}

func NewSearchBackend(cfg SearchConfig) *SearchBackend {
	if cfg.Timeout <= 0 {
		cfg.Timeout = 15 * time.Second
	}
	if cfg.Provider == "" || cfg.Provider == "auto" {
		if cfg.APIKey != "" {
			cfg.Provider = "tavily"
		} else if cfg.UpstreamURL != "" {
			cfg.Provider = "searxng"
		} else {
			cfg.Provider = "ddg_lite"
		}
	}
	if cfg.UpstreamURL == "" {
		switch cfg.Provider {
		case "tavily":
			cfg.UpstreamURL = "https://api.tavily.com/search"
		case "ddg_lite":
			cfg.UpstreamURL = "https://html.duckduckgo.com/html/"
		}
	}
	return &SearchBackend{
		cfg:    cfg,
		client: &http.Client{Timeout: cfg.Timeout},
	}
}

func (b *SearchBackend) ProviderName() string { return b.cfg.Provider }
func (b *SearchBackend) HeadlessConfigured() bool {
	return strings.TrimSpace(b.cfg.HeadlessURL) != ""
}

func applySiteFilter(query string, domains []string) string {
	var parts []string
	for _, d := range domains {
		clean := strings.TrimSpace(strings.ToLower(d))
		if clean != "" {
			parts = append(parts, "site:"+clean)
		}
		if len(parts) >= 5 {
			break
		}
	}
	if len(parts) == 0 {
		return query
	}
	return fmt.Sprintf("(%s) (%s)", query, strings.Join(parts, " OR "))
}

func filterDomains(rows []SearchResult, includeDomains []string) []SearchResult {
	var cleanDomains []string
	for _, d := range includeDomains {
		c := strings.TrimPrefix(strings.TrimSpace(strings.ToLower(d)), ".")
		if c != "" {
			cleanDomains = append(cleanDomains, c)
		}
	}
	if len(cleanDomains) == 0 {
		return rows
	}
	out := make([]SearchResult, 0, len(rows))
	for _, row := range rows {
		for _, d := range cleanDomains {
			if matchesDomain(row.Domain, d) {
				out = append(out, row)
				break
			}
		}
	}
	return out
}

func (b *SearchBackend) Search(ctx context.Context, query string, maxResults int, includeDomains []string, searchDepth string) ([]SearchResult, error) {
	switch b.cfg.Provider {
	case "tavily":
		payload := map[string]any{
			"api_key":      b.cfg.APIKey,
			"query":        query,
			"max_results":  maxResults,
			"search_depth": searchDepth,
		}
		if len(includeDomains) > 0 {
			payload["include_domains"] = includeDomains
		}
		raw, _ := json.Marshal(payload)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.cfg.UpstreamURL, bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := b.client.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("tavily upstream HTTP %d", resp.StatusCode)
		}
		var wire struct {
			Results []struct {
				Title   string   `json:"title"`
				URL     string   `json:"url"`
				Content string   `json:"content"`
				Snippet string   `json:"snippet"`
				Score   *float64 `json:"score"`
			} `json:"results"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&wire); err != nil {
			return nil, err
		}
		out := make([]SearchResult, 0, len(wire.Results))
		for _, r := range wire.Results {
			if r.URL == "" {
				continue
			}
			snip := r.Content
			if snip == "" {
				snip = r.Snippet
			}
			dom := ""
			if u, perr := url.Parse(r.URL); perr == nil {
				dom = strings.ToLower(u.Hostname())
			}
			out = append(out, SearchResult{Title: r.Title, URL: r.URL, Snippet: snip, Domain: dom, Score: r.Score})
		}
		return filterDomains(out, includeDomains), nil

	case "searxng":
		scoped := applySiteFilter(query, includeDomains)
		u := fmt.Sprintf("%s/search?%s", strings.TrimRight(b.cfg.UpstreamURL, "/"), url.Values{"q": {scoped}, "format": {"json"}}.Encode())
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		if err != nil {
			return nil, err
		}
		resp, err := b.client.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("searxng upstream HTTP %d", resp.StatusCode)
		}
		var wire struct {
			Results []struct {
				Title   string `json:"title"`
				URL     string `json:"url"`
				Content string `json:"content"`
			} `json:"results"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&wire); err != nil {
			return nil, err
		}
		out := make([]SearchResult, 0, len(wire.Results))
		for _, r := range wire.Results {
			if r.URL == "" {
				continue
			}
			dom := ""
			if pu, perr := url.Parse(r.URL); perr == nil {
				dom = strings.ToLower(pu.Hostname())
			}
			out = append(out, SearchResult{Title: r.Title, URL: r.URL, Snippet: r.Content, Domain: dom})
		}
		return filterDomains(out, includeDomains), nil

	default:
		scoped := applySiteFilter(query, includeDomains)
		u := fmt.Sprintf("%s?%s", b.cfg.UpstreamURL, url.Values{"q": {scoped}}.Encode())
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("User-Agent", "Mozilla/5.0 (compatible; agentruntime-crawler/1.0)")
		req.Header.Set("Accept", "text/html")
		resp, err := b.client.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("ddg upstream HTTP %d", resp.StatusCode)
		}
		raw, _ := readLimited(resp.Body, 512*1024)
		return filterDomains(ParseDDGHTML(string(raw)), includeDomains), nil
	}
}

// RenderJS delegates dynamic SPA rendering to an external headless service if configured.
func (b *SearchBackend) RenderJS(ctx context.Context, targetURL string) (htmlBody string, used bool, err error) {
	base := strings.TrimRight(strings.TrimSpace(b.cfg.HeadlessURL), "/")
	if base == "" {
		return "", false, nil
	}
	payload, _ := json.Marshal(map[string]any{"url": targetURL})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/render", bytes.NewReader(payload))
	if err != nil {
		return "", false, err
	}
	req.Header.Set("Content-Type", "application/json")
	if tok := strings.TrimSpace(b.cfg.HeadlessTok); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := b.client.Do(req)
	if err != nil {
		return "", false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", false, fmt.Errorf("headless renderer HTTP %d", resp.StatusCode)
	}
	var wire struct {
		HTML string `json:"html"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&wire); err != nil {
		return "", false, err
	}
	return wire.HTML, true, nil
}
