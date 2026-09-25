// Minimal Travis CI API v3 client. Travis API tokens carry the full permissions of
// their owner (no scopes, no read-only tokens), so this client is where access is
// narrowed: any path can be read, but the only writes it will ever send are the
// build restart/cancel actions listed in WRITE_PATHS.

const DEFAULT_API_URL = 'https://api.travis-ci.com';
const DEFAULT_WEB_URL = 'https://app.travis-ci.com';

export const WRITE_ACTIONS = ['restart', 'cancel'];
const WRITE_PATHS = new RegExp(`^/build/\\d+/(${WRITE_ACTIONS.join('|')})$`);

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

  get(path, { query = {}, accept = 'application/json' } = {}) {
    return this.request('GET', path, { query, accept });
  }

  post(path) {
    if (!WRITE_PATHS.test(path)) {
      throw new Error(`Refusing write to ${path}: only build restart/cancel are allowed`);
    }
    return this.request('POST', path, {});
  }

  async request(method, path, { query = {}, accept = 'application/json' }) {
    const url = new URL(this.apiUrl + path);
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value));
      }
    }

    const response = await this.fetch(url, {
      method,
      headers: {
        'Travis-API-Version': '3',
        Authorization: `token ${this.token}`,
        Accept: accept,
        'User-Agent': 'travis-mcp',
      },
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Travis API ${response.status} on ${method} ${url.pathname}: ${body.slice(0, 500)}`);
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

  restartBuild(buildId) {
    return this.post(`/build/${encodeURIComponent(buildId)}/restart`);
  }

  cancelBuild(buildId) {
    return this.post(`/build/${encodeURIComponent(buildId)}/cancel`);
  }

  buildWebUrl(repoSlug, buildId) {
    return `${this.webUrl}/github/${repoSlug}/builds/${buildId}`;
  }

  jobWebUrl(repoSlug, jobId) {
    return `${this.webUrl}/github/${repoSlug}/jobs/${jobId}`;
  }
}
