import type { HandlerDeps } from "./handlers";
import { deliverCancelled, deliverFailed } from "./handlers";
import { RunNotFoundError } from "./github";
import { humanEta } from "./utils";

const DISPATCH_STALE_SECONDS = 10 * 60;
const RUN_MISSING_SECONDS = 12 * 60 * 60;

/**
 * Poll active GitHub Actions runs. The worker normally posts its own final
 * card; this only fills the gap for cancelled runs and workers that died
 * before they could report anything.
 */
export async function refreshJobs(deps: HandlerDeps): Promise<void> {
  const jobs = await deps.store.list();
  if (jobs.length === 0) return;
  const now = deps.now ? deps.now() : Math.floor(Date.now() / 1000);

  for (const job of jobs) {
    if (!job.runId) {
      if (now - job.createdAt > DISPATCH_STALE_SECONDS) {
        await deliverFailed(deps, {
          ...job,
          error: "GitHub Actions never started this job.",
        });
      }
      continue;
    }

    let info;
    try {
      info = await deps.gh.view(job.runId);
    } catch (error) {
      if (now - job.createdAt > RUN_MISSING_SECONDS) {
        await deliverFailed(deps, {
          ...job,
          error:
            error instanceof RunNotFoundError
              ? "The GitHub Actions run disappeared."
              : "Could not read the GitHub Actions run for 12 hours.",
        });
        continue;
      }
      if (error instanceof RunNotFoundError) continue;
      console.log(
        JSON.stringify({
          level: "warn",
          msg: "Could not refresh job",
          job_id: job.id,
          run_id: job.runId,
          error: error instanceof Error ? error.message : String(error),
        }),
      );
      continue;
    }

    const conclusion = String(info.conclusion || "").toLowerCase();
    if (info.status === "completed") {
      if (conclusion === "cancelled" || (job.state === "cancelling" && conclusion !== "success")) {
        await deliverCancelled(deps, job);
      } else if (conclusion === "success") {
        await deps.store.remove(job.id);
      } else {
        await deliverFailed(deps, job);
      }
      continue;
    }

    const detail =
      job.state === "cancelling"
        ? "Cancelling on GitHub Actions…"
        : info.status === "in_progress"
          ? `GitHub Actions · ${humanEta(Math.max(0, now - job.startedAt))}`
          : "Waiting for a GitHub Actions runner…";
    await deps.store.update(job.id, {
      state: job.state === "cancelling" ? "cancelling" : info.status === "in_progress" ? "running" : "queued",
      detail,
    });
  }
}
