import type { HandlerDeps } from "./handlers";
import { deliverCancelled, deliverFailed, editJobCard } from "./handlers";
import { RunNotFoundError } from "./github";
import { cancelKeyboard, renderJob } from "./render";

const DISPATCH_STALE_SECONDS = 10 * 60;
const RUN_MISSING_SECONDS = 12 * 60 * 60;

/**
 * Poll active GitHub Actions runs. The worker normally posts its own progress
 * and final card; this keeps queued cards alive, reports the switch to
 * "running", and fills the gap for cancelled runs and workers that died
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
          ? "Runner started · preparing the job…"
          : "Waiting for a GitHub Actions runner…";
    const previousState = job.state;
    const nextState =
      job.state === "cancelling"
        ? "cancelling"
        : info.status === "in_progress"
          ? "running"
          : "queued";
    const updated = await deps.store.update(job.id, {
      state: nextState,
      detail,
    });
    if (updated && (nextState !== previousState || nextState === "queued")) {
      try {
        await editJobCard(
          deps,
          updated,
          renderJob(updated, now),
          cancelKeyboard(job.id),
        );
      } catch {
        // The worker or the user may have already updated or removed the card.
      }
    }
  }
}
