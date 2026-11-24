export class GitHubError extends Error {
  constructor(
    message: string,
    public readonly status = 0,
  ) {
    super(message);
    this.name = "GitHubError";
  }
}

export class RunNotFoundError extends GitHubError {
  constructor(runId: number) {
    super(`GitHub Actions run ${runId} was not found.`, 404);
    this.name = "RunNotFoundError";
  }
}

export interface GitHubConfig {
  token: string;
  repo: string;
  ref: string;
  workflow: string;
}

export interface RunInfo {
  status: string;
  conclusion: string | null;
  htmlUrl: string;
}

export class GitHubAPI {
  private readonly config: GitHubConfig;
  private readonly fetchImpl: typeof fetch;

  constructor(config: GitHubConfig, fetchImpl: typeof fetch = fetch) {
    this.config = config;
    this.fetchImpl = ((...args: Parameters<typeof fetch>) =>
      fetchImpl.call(globalThis, ...args)) as typeof fetch;
  }

  private repoPath(): string {
    return this.config.repo
      .split("/")
      .map((part) => encodeURIComponent(part))
      .join("/");
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set("authorization", `Bearer ${this.config.token}`);
    headers.set("accept", "application/vnd.github+json");
    headers.set("x-github-api-version", "2022-11-28");
    headers.set("user-agent", "mirror-bot-cloudflare");
    if (init.body) headers.set("content-type", "application/json");

    let response: Response;
    try {
      response = await this.fetchImpl(`https://api.github.com${path}`, {
        ...init,
        headers,
      });
    } catch (error) {
      throw new GitHubError(
        `GitHub request failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    return response;
  }

  async dispatch(
    payload: string,
    jobId: string,
  ): Promise<{ runId: number; runUrl: string }> {
    const workflow = encodeURIComponent(this.config.workflow);
    const response = await this.request(
      `/repos/${this.repoPath()}/actions/workflows/${workflow}/dispatches`,
      {
        method: "POST",
        body: JSON.stringify({
          ref: this.config.ref,
          inputs: { payload, job_id: jobId },
          return_run_details: true,
        }),
      },
    );

    if (!response.ok) {
      const detail = (await response.text()).slice(0, 500);
      throw new GitHubError(
        `GitHub refused the workflow dispatch (HTTP ${response.status})${detail ? `: ${detail}` : ""}.`,
        response.status,
      );
    }

    const data = (await response.json().catch(() => null)) as
      | { workflow_run_id?: number; html_url?: string }
      | null;
    const runId = Number(data?.workflow_run_id || 0);
    if (!runId) {
      throw new GitHubError(
        "GitHub did not return the new workflow run id. Check that the token can dispatch workflows.",
        response.status,
      );
    }
    return { runId, runUrl: String(data?.html_url || "") };
  }

  async cancel(runId: number): Promise<boolean> {
    const response = await this.request(
      `/repos/${this.repoPath()}/actions/runs/${runId}/cancel`,
      { method: "POST" },
    );
    if (response.status === 202 || response.status === 204) return true;
    if (response.status === 404 || response.status === 409) return false;
    const detail = (await response.text()).slice(0, 300);
    throw new GitHubError(
      `GitHub refused to cancel run ${runId} (HTTP ${response.status})${detail ? `: ${detail}` : ""}.`,
      response.status,
    );
  }

  async view(runId: number): Promise<RunInfo> {
    const response = await this.request(
      `/repos/${this.repoPath()}/actions/runs/${runId}`,
      { method: "GET" },
    );
    if (response.status === 404) throw new RunNotFoundError(runId);
    if (!response.ok) {
      const detail = (await response.text()).slice(0, 300);
      throw new GitHubError(
        `Could not read run ${runId} (HTTP ${response.status})${detail ? `: ${detail}` : ""}.`,
        response.status,
      );
    }
    const data = (await response.json().catch(() => null)) as
      | { status?: string; conclusion?: string | null; html_url?: string }
      | null;
    if (!data) throw new GitHubError(`GitHub returned an empty run ${runId}.`);
    return {
      status: String(data.status || ""),
      conclusion: data.conclusion ? String(data.conclusion) : null,
      htmlUrl: String(data.html_url || ""),
    };
  }
}
