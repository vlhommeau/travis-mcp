# travis-mcp

Read-only [MCP](https://modelcontextprotocol.io) server for Travis CI, over the
[Travis API v3](https://developer.travis-ci.com/). Lets AI coding agents check build
status, find the failing job and read its log — nothing else.

## Why read-only lives in the server

Travis API tokens have no scopes: the API docs state a token can be used "to do anything
that you can do using the web interface" (restart/cancel/trigger builds, edit settings and
env vars). Read-only access therefore cannot come from the token and is enforced here:

- the HTTP client only issues `GET` requests (covered by a test);
- only the four tools below are exposed — no trigger/restart/cancel, no env vars, no
  settings, no caches.

## Tools

| Tool | Travis endpoint | Purpose |
|------|-----------------|---------|
| `get_build` | `GET /build/{id}` | State, branch/PR, commit, timing, job IDs, web URL |
| `get_build_jobs` | `GET /build/{id}/jobs` | Per-job state and stage, to spot the failing job |
| `list_builds` | `GET /repo/{slug}/builds` | Recent builds, filtered by branch, event type, state or PR number |
| `get_job_log` | `GET /job/{id}/log.txt` | Job log, ANSI stripped, tail (default 200 lines) or `grep` with context |

A job may have no stored log (Travis then returns a literal `null` body, e.g. a log not yet
archived or removed); `get_job_log` reports it as unavailable rather than returning `null`.

The API has no pull request filter, so `list_builds` with `pull_request` pages through the
last 500 PR builds and filters client side.

## Configuration

| Variable | Required | Default |
|----------|----------|---------|
| `TRAVIS_API_TOKEN` | yes | — |
| `TRAVIS_DEFAULT_REPO` | no | — (`owner/name` used when `list_builds` gets no `repo`) |
| `TRAVIS_API_URL` | no | `https://api.travis-ci.com` |
| `TRAVIS_WEB_URL` | no | `https://app.travis-ci.com` |

Get the token from <https://app.travis-ci.com/account/preferences> → **API authentication**
(works for accounts that sign in with GitHub), or with the Travis CLI:
`travis login --pro && travis token --pro`. The *Assets* and *RSS* tokens on the same page
do not give access to builds or logs.

Keep the token out of tracked files: store it in the OS keychain or a secrets manager and
export it into the environment the MCP client is launched from.

## Usage

Claude Code `.mcp.json`, pinned to a tag:

```json
{
  "mcpServers": {
    "travis": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "github:vlhommeau/travis-mcp#v0.1.0"],
      "env": {
        "TRAVIS_API_TOKEN": "${TRAVIS_API_TOKEN}",
        "TRAVIS_DEFAULT_REPO": "owner/name"
      }
    }
  }
}
```

No build step: the server is plain ESM JavaScript, runnable straight from a git checkout
(Node.js 20+).

## Development

```sh
npm install
npm test
TRAVIS_API_TOKEN=... npx @modelcontextprotocol/inspector node src/index.js
```
