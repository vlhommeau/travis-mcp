// Minimal Travis CI API v3 client. GET only, by construction: Travis API tokens
// carry the full permissions of their owner (no scopes, no read-only tokens), so
// this client is where read-only access is enforced.

const DEFAULT_API_URL = 'https://api.travis-ci.com';
const DEFAULT_WEB_URL = 'https://app.travis-ci.com';

export class TravisClient {
  constructor({ token, apiUrl = DEFAULT_API_URL, webUrl = DEFAULT_WEB_URL, fetchImpl = fetch }) {
    if (!token) {
      throw new Error('TRAVIS_API_TOKEN is not set');
    }
    this.token = token;
    this.apiUrl = apiUrl.replace(/\/+$/, '');
    this.webUrl = webUrl.replace(/\/+$/, '');
    this.fetch = fetchImpl;
  }

  async get(path, { query = {}, accept = 'application/json' } = {}) {
    const url = new URL(this.apiUrl + path);
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value));
      }
    }

    const response = await this.fetch(url, {
      method: 'GET',
      headers: {
        'Travis-API-Version': '3',
        Authorization: `token ${this.token}`,
        Accept: accept,
        'User-Agent': 'travis-mcp',
      },
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Travis API ${response.status} on GET ${url.pathname}: ${body.slice(0, 500)}`);
    }

    return accept === 'application/json' ? response.json() : response.text();
  }

  getBuild(buildId) {
    return this.get(`/build/${encodeURIComponent(buildId)}`, {
      query: { include: 'build.commit,build.repository' },
    });
  }

  getBuildJobs(buildId) {
    return this.get(`/build/${encodeURIComponent(buildId)}/jobs`, {
      query: { include: 'job.stage' },
    });
  }

  listBuilds(repoSlug, { branch, eventType, state, limit, offset }) {
    return this.get(`/repo/${encodeURIComponent(repoSlug)}/builds`, {
      query: {
        'branch.name': branch,
        event_type: eventType,
        state,
        limit,
        offset,
        sort_by: 'id:desc',
        include: 'build.commit',
      },
    });
  }

  getJobLog(jobId) {
    return this.get(`/job/${encodeURIComponent(jobId)}/log.txt`, { accept: 'text/plain' });
  }

  buildWebUrl(repoSlug, buildId) {
    return `${this.webUrl}/github/${repoSlug}/builds/${buildId}`;
  }

  jobWebUrl(repoSlug, jobId) {
    return `${this.webUrl}/github/${repoSlug}/jobs/${jobId}`;
  }
}
