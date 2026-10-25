package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// 各家 Git 平台不同的地方：账号信息、仓库列表、仓库地址的形状、推送时的 Basic 认证、开 PR/MR。
// 草稿：GitHub 用 draft 字段（仓库不支持时退回普通 PR），GitLab 用标题前缀 Draft:，Gitea 用 WIP:，Bitbucket 试 draft 字段、
// 不认就退回普通 PR，Gitee 开普通 PR。

// remoteUser 是平台上的账号。ID 是平台给的不变的编号（登录名会改），用来认出同一个人。
type remoteUser struct {
	ID, Login, Name, AvatarURL, Email string
}

func (p *Provider) user(ctx context.Context, token string) (remoteUser, error) {
	switch p.id {
	case "github", "gitee", "gitea":
		var u struct {
			ID        json.Number `json:"id"`
			Login     string      `json:"login"`
			Name      string      `json:"name"`
			FullName  string      `json:"full_name"`
			AvatarURL string      `json:"avatar_url"`
			Email     string      `json:"email"`
		}
		if err := p.api(ctx, token, http.MethodGet, "/user", nil, &u); err != nil {
			return remoteUser{}, err
		}
		name := u.Name
		if name == "" {
			name = u.FullName
		}
		return remoteUser{ID: u.ID.String(), Login: u.Login, Name: name, AvatarURL: u.AvatarURL, Email: u.Email}, nil
	case "gitlab":
		var u struct {
			ID          json.Number `json:"id"`
			Username    string      `json:"username"`
			Name        string      `json:"name"`
			AvatarURL   string      `json:"avatar_url"`
			PublicEmail string      `json:"public_email"`
		}
		if err := p.api(ctx, token, http.MethodGet, "/user", nil, &u); err != nil {
			return remoteUser{}, err
		}
		return remoteUser{ID: u.ID.String(), Login: u.Username, Name: u.Name, AvatarURL: u.AvatarURL, Email: u.PublicEmail}, nil
	case "bitbucket":
		var u struct {
			UUID        string `json:"uuid"`
			Username    string `json:"username"`
			Nickname    string `json:"nickname"`
			DisplayName string `json:"display_name"`
			Links       struct {
				Avatar struct {
					Href string `json:"href"`
				} `json:"avatar"`
			} `json:"links"`
		}
		if err := p.api(ctx, token, http.MethodGet, "/user", nil, &u); err != nil {
			return remoteUser{}, err
		}
		login := u.Username
		if login == "" {
			login = u.Nickname
		}
		return remoteUser{ID: u.UUID, Login: login, Name: u.DisplayName, AvatarURL: u.Links.Avatar.Href}, nil
	case "google":
		var u struct {
			Sub           string `json:"sub"`
			Email         string `json:"email"`
			EmailVerified bool   `json:"email_verified"`
			Name          string `json:"name"`
			Picture       string `json:"picture"`
		}
		if err := p.api(ctx, token, http.MethodGet, "/userinfo", nil, &u); err != nil {
			return remoteUser{}, err
		}
		if !u.EmailVerified {
			return remoteUser{}, &GitError{Provider: p.Name, Status: http.StatusForbidden, Message: "这个 Google 账号的邮箱还没有验证"}
		}
		login, _, _ := strings.Cut(u.Email, "@")
		return remoteUser{ID: u.Sub, Login: login, Name: u.Name, AvatarURL: u.Picture, Email: u.Email}, nil
	}
	return remoteUser{}, fmt.Errorf("不认识的登录方式 %s", p.id)
}

// remoteRepo 是“我的仓库”里的一项，各家统一成这个形状。
type remoteRepo struct {
	Provider      string `json:"provider"`
	FullName      string `json:"full_name"`
	CloneURL      string `json:"clone_url"`
	Private       bool   `json:"private"`
	DefaultBranch string `json:"default_branch"`
	CanPush       bool   `json:"can_push"`
}

// repoPages 是“我的仓库”最多翻几页：每页 100 个（Gitea 50 个），再多的用户自己贴地址。
const repoPages = 5

// repos 列出用户能看到的仓库（最近活动的在前），一页一页翻，直到某页不满或翻满 repoPages 页。
func (p *Provider) repos(ctx context.Context, token string) ([]remoteRepo, error) {
	out := []remoteRepo{}
	for page := 1; page <= repoPages; page++ {
		got, full, err := p.repoPage(ctx, token, page)
		if err != nil {
			return nil, err
		}
		for _, r := range got {
			r.Provider = p.id
			out = append(out, r)
		}
		if !full {
			break
		}
	}
	return out, nil
}

// repoPage 取“我的仓库”的第 page 页；full 表示这一页是满的，后面可能还有。
func (p *Provider) repoPage(ctx context.Context, token string, page int) (out []remoteRepo, full bool, err error) {
	n := strconv.Itoa(page)
	switch p.id {
	case "github", "gitea":
		var rs []struct {
			FullName      string `json:"full_name"`
			CloneURL      string `json:"clone_url"`
			Private       bool   `json:"private"`
			DefaultBranch string `json:"default_branch"`
			Permissions   struct {
				Push bool `json:"push"`
			} `json:"permissions"`
		}
		size, path := 100, "/user/repos?sort=updated&per_page=100&affiliation=owner,collaborator,organization_member&page="+n
		if p.id == "gitea" {
			size, path = 50, "/user/repos?limit=50&page="+n
		}
		if err := p.api(ctx, token, http.MethodGet, path, nil, &rs); err != nil {
			return nil, false, err
		}
		for _, r := range rs {
			out = append(out, remoteRepo{FullName: r.FullName, CloneURL: r.CloneURL, Private: r.Private, DefaultBranch: r.DefaultBranch, CanPush: r.Permissions.Push})
		}
		return out, len(rs) >= size, nil
	case "gitee":
		var rs []struct {
			FullName      string `json:"full_name"`
			HTMLURL       string `json:"html_url"`
			Private       bool   `json:"private"`
			DefaultBranch string `json:"default_branch"`
			Permission    struct {
				Push bool `json:"push"`
			} `json:"permission"`
		}
		if err := p.api(ctx, token, http.MethodGet, "/user/repos?sort=updated&per_page=100&page="+n, nil, &rs); err != nil {
			return nil, false, err
		}
		for _, r := range rs {
			out = append(out, remoteRepo{FullName: r.FullName, CloneURL: strings.TrimSuffix(r.HTMLURL, "/") + ".git", Private: r.Private, DefaultBranch: r.DefaultBranch, CanPush: r.Permission.Push})
		}
		return out, len(rs) >= 100, nil
	case "gitlab":
		var rs []struct {
			PathWithNamespace string `json:"path_with_namespace"`
			HTTPURLToRepo     string `json:"http_url_to_repo"`
			Visibility        string `json:"visibility"`
			DefaultBranch     string `json:"default_branch"`
		}
		// min_access_level=30（Developer）：能推送的才列出来。
		if err := p.api(ctx, token, http.MethodGet, "/projects?membership=true&min_access_level=30&order_by=last_activity_at&per_page=100&page="+n, nil, &rs); err != nil {
			return nil, false, err
		}
		for _, r := range rs {
			out = append(out, remoteRepo{FullName: r.PathWithNamespace, CloneURL: r.HTTPURLToRepo, Private: r.Visibility != "public", DefaultBranch: r.DefaultBranch, CanPush: true})
		}
		return out, len(rs) >= 100, nil
	case "bitbucket":
		var res struct {
			Values []struct {
				FullName   string `json:"full_name"`
				IsPrivate  bool   `json:"is_private"`
				MainBranch struct {
					Name string `json:"name"`
				} `json:"mainbranch"`
				Links struct {
					Clone []struct {
						Name string `json:"name"`
						Href string `json:"href"`
					} `json:"clone"`
				} `json:"links"`
			} `json:"values"`
			Next string `json:"next"`
		}
		// role=contributor：有写权限的才列出来。
		if err := p.api(ctx, token, http.MethodGet, "/repositories?role=contributor&pagelen=100&sort=-updated_on&page="+n, nil, &res); err != nil {
			return nil, false, err
		}
		for _, r := range res.Values {
			clone := p.Web + "/" + r.FullName + ".git"
			for _, c := range r.Links.Clone {
				if c.Name == "https" {
					if u, err := url.Parse(c.Href); err == nil {
						u.User = nil // Bitbucket 的地址里带着用户名
						clone = u.String()
					}
				}
			}
			out = append(out, remoteRepo{FullName: r.FullName, CloneURL: clone, Private: r.IsPrivate, DefaultBranch: r.MainBranch.Name, CanPush: true})
		}
		return out, res.Next != "", nil
	}
	return nil, false, &GitError{Provider: p.Name, Status: http.StatusBadRequest, Message: p.Name + " 不是 Git 平台"}
}

// repoOf 认出这一家的仓库地址（https，主机与网页地址相同），返回仓库路径：owner/repo，GitLab 可以有多级群组。
func (p *Provider) repoOf(raw string) (string, bool) {
	if !p.git {
		return "", false
	}
	u, err := url.Parse(raw)
	web, _ := url.Parse(p.Web)
	if err != nil || web == nil || !strings.EqualFold(u.Host, web.Host) || u.Scheme != web.Scheme {
		return "", false
	}
	path := strings.Trim(strings.TrimSuffix(strings.TrimPrefix(u.Path, strings.TrimRight(web.Path, "/")), ".git"), "/")
	parts := strings.Split(path, "/")
	if len(parts) < 2 || (p.id != "gitlab" && len(parts) != 2) {
		return "", false
	}
	for _, part := range parts {
		if part == "" || part == "." || part == ".." {
			return "", false
		}
	}
	return path, true
}

// gitAuth 是推送、拉取时的 Basic 认证（经环境变量传给 git，不进命令行与 .git/config）。
// GitHub 把令牌放用户名、密码填 x-oauth-basic；GitLab、Gitea 用户名填 oauth2；Bitbucket 填 x-token-auth；
// Gitee 用登录名加令牌（Gitee 的 HTTPS 认证是“用户名 + 密码或令牌”）。
func (p *Provider) gitAuth(c Connection, token string) (string, string) {
	switch p.id {
	case "github":
		return token, "x-oauth-basic"
	case "gitlab", "gitea":
		return "oauth2", token
	case "bitbucket":
		return "x-token-auth", token
	default:
		return c.Login, token
	}
}

// commitAuthor 是推送时提交的作者：GitHub 用它的 noreply 地址；其余用公开邮箱，没有时用 <登录名>@users.noreply.<主机>。
func (p *Provider) commitAuthor(c Connection) (string, string) {
	name := c.Login
	if p.id == "github" {
		return name, fmt.Sprintf("%s+%s@users.noreply.github.com", c.RemoteID, c.Login)
	}
	if c.Email != "" {
		return name, c.Email
	}
	host := strings.TrimPrefix(strings.TrimPrefix(p.Web, "https://"), "http://")
	host, _, _ = strings.Cut(host, "/")
	return name, c.Login + "@users.noreply." + host
}

// requestName 是这一家对“合并请求”的叫法。
func (p *Provider) requestName() string {
	if p.id == "gitlab" {
		return "Merge Request"
	}
	return "PR"
}

type pullRequest struct {
	HTMLURL string `json:"html_url"`
	Number  int    `json:"number"`
}

// openPR 开草稿 PR/MR；同一分支已有打开的就直接用它。
func (p *Provider) openPR(ctx context.Context, token, repo, title, head, base, body string) (pullRequest, error) {
	var ge *GitError
	switch p.id {
	case "github", "gitee", "gitea":
		req := map[string]any{"title": title, "head": head, "base": base, "body": body}
		switch p.id {
		case "github":
			req["draft"] = true
		case "gitea":
			req["title"] = "WIP: " + title
		}
		var pr pullRequest
		err := p.api(ctx, token, http.MethodPost, "/repos/"+repo+"/pulls", req, &pr)
		if p.id == "github" && errors.As(err, &ge) && ge.Status == http.StatusUnprocessableEntity && strings.Contains(strings.ToLower(ge.Message), "draft") {
			delete(req, "draft") // 仓库不支持草稿：退回普通 PR
			err = p.api(ctx, token, http.MethodPost, "/repos/"+repo+"/pulls", req, &pr)
		}
		if err != nil && errors.As(err, &ge) && ge.Status >= 400 && ge.Status < 500 && ge.Status != http.StatusUnauthorized {
			if existing, ok := p.existingPR(ctx, token, repo, head); ok {
				return existing, nil
			}
		}
		return pr, err
	case "gitlab":
		project := "/projects/" + url.PathEscape(repo)
		var mr struct {
			WebURL string `json:"web_url"`
			IID    int    `json:"iid"`
		}
		err := p.api(ctx, token, http.MethodPost, project+"/merge_requests",
			map[string]any{"source_branch": head, "target_branch": base, "title": "Draft: " + title, "description": body}, &mr)
		if errors.As(err, &ge) && ge.Status == http.StatusConflict {
			if existing, ok := p.existingPR(ctx, token, repo, head); ok {
				return existing, nil
			}
		}
		return pullRequest{HTMLURL: mr.WebURL, Number: mr.IID}, err
	case "bitbucket":
		req := map[string]any{
			"title": title, "description": body, "draft": true,
			"source": map[string]any{"branch": map[string]string{"name": head}}, "destination": map[string]any{"branch": map[string]string{"name": base}},
		}
		var pr struct {
			ID    int `json:"id"`
			Links struct {
				HTML struct {
					Href string `json:"href"`
				} `json:"html"`
			} `json:"links"`
		}
		path := "/repositories/" + repo + "/pullrequests"
		err := p.api(ctx, token, http.MethodPost, path, req, &pr)
		if errors.As(err, &ge) && ge.Status == http.StatusBadRequest && strings.Contains(strings.ToLower(ge.Message), "draft") {
			delete(req, "draft")
			err = p.api(ctx, token, http.MethodPost, path, req, &pr)
		}
		if err != nil && errors.As(err, &ge) && ge.Status == http.StatusBadRequest {
			if existing, ok := p.existingPR(ctx, token, repo, head); ok {
				return existing, nil
			}
		}
		return pullRequest{HTMLURL: pr.Links.HTML.Href, Number: pr.ID}, err
	}
	return pullRequest{}, &GitError{Provider: p.Name, Status: http.StatusBadRequest, Message: p.Name + " 不是 Git 平台"}
}

// existingPR 找同一分支上已经打开的 PR/MR。
func (p *Provider) existingPR(ctx context.Context, token, repo, head string) (pullRequest, bool) {
	owner, _, _ := strings.Cut(repo, "/")
	switch p.id {
	case "github", "gitee":
		var prs []pullRequest
		q := url.Values{"head": {owner + ":" + head}, "state": {"open"}}
		if p.api(ctx, token, http.MethodGet, "/repos/"+repo+"/pulls?"+q.Encode(), nil, &prs) == nil && len(prs) > 0 {
			return prs[0], true
		}
	case "gitea":
		var prs []struct {
			pullRequest
			Head struct {
				Ref string `json:"ref"`
			} `json:"head"`
		}
		if p.api(ctx, token, http.MethodGet, "/repos/"+repo+"/pulls?state=open", nil, &prs) == nil {
			for _, pr := range prs {
				if pr.Head.Ref == head {
					return pr.pullRequest, true
				}
			}
		}
	case "gitlab":
		var mrs []struct {
			WebURL string `json:"web_url"`
			IID    int    `json:"iid"`
		}
		q := url.Values{"state": {"opened"}, "source_branch": {head}}
		if p.api(ctx, token, http.MethodGet, "/projects/"+url.PathEscape(repo)+"/merge_requests?"+q.Encode(), nil, &mrs) == nil && len(mrs) > 0 {
			return pullRequest{HTMLURL: mrs[0].WebURL, Number: mrs[0].IID}, true
		}
	case "bitbucket":
		var page struct {
			Values []struct {
				ID    int `json:"id"`
				Links struct {
					HTML struct {
						Href string `json:"href"`
					} `json:"html"`
				} `json:"links"`
			} `json:"values"`
		}
		q := url.Values{"state": {"OPEN"}, "q": {`source.branch.name="` + head + `"`}}
		if p.api(ctx, token, http.MethodGet, "/repositories/"+repo+"/pullrequests?"+q.Encode(), nil, &page) == nil && len(page.Values) > 0 {
			return pullRequest{HTMLURL: page.Values[0].Links.HTML.Href, Number: page.Values[0].ID}, true
		}
	}
	return pullRequest{}, false
}

// remoteID 统一成字符串（GitHub 的数字编号、Bitbucket 的 UUID、Google 的 sub）。
func remoteID(n int64) string { return strconv.FormatInt(n, 10) }
