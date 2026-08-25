import { z } from "zod/v4";
import type { D1DatabaseBinding } from "./d1";

export interface JobFinderRunLock {
  acquire(workflowInstanceId: string, acquiredAt: string): Promise<RunLockAcquisition>;
  release(workflowInstanceId: string): Promise<boolean>;
}

export type RunLockAcquisition =
  | { kind: "acquired"; workflowInstanceId: string; acquiredAt: string }
  | { kind: "contended"; workflowInstanceId: string; acquiredAt: string };

const RunLockRowSchema = z.object({
  workflow_instance_id: z.string().min(1),
  acquired_at: z.iso.datetime(),
});

const ACQUIRE_RUN_LOCK_SQL = `
  INSERT INTO job_finder_run_lock (singleton, workflow_instance_id, acquired_at)
  VALUES (1, ?, ?)
  ON CONFLICT(singleton) DO UPDATE SET
    workflow_instance_id = job_finder_run_lock.workflow_instance_id,
    acquired_at = job_finder_run_lock.acquired_at
  RETURNING workflow_instance_id, acquired_at
`;

const RELEASE_RUN_LOCK_SQL = `
  DELETE FROM job_finder_run_lock
  WHERE singleton = 1 AND workflow_instance_id = ?
  RETURNING workflow_instance_id, acquired_at
`;

export function createD1JobFinderRunLock(binding: D1DatabaseBinding): JobFinderRunLock {
  return {
    async acquire(workflowInstanceId, acquiredAt) {
      const owner = RunLockRowSchema.parse(
        await binding.prepare(ACQUIRE_RUN_LOCK_SQL).bind(workflowInstanceId, acquiredAt).first(),
      );
      return owner.workflow_instance_id === workflowInstanceId
        ? {
            kind: "acquired",
            workflowInstanceId: owner.workflow_instance_id,
            acquiredAt: owner.acquired_at,
          }
        : {
            kind: "contended",
            workflowInstanceId: owner.workflow_instance_id,
            acquiredAt: owner.acquired_at,
          };
    },
    async release(workflowInstanceId) {
      return (
        RunLockRowSchema.nullable().parse(
          await binding.prepare(RELEASE_RUN_LOCK_SQL).bind(workflowInstanceId).first(),
        ) !== null
      );
    },
  };
}
