import { AsyncLocalStorage } from "node:async_hooks";
import pino from "pino";
import type { PinoLogLevel } from "./config/schema";

interface RunLogContext {
  runId: string;
}

const runLogContext = new AsyncLocalStorage<RunLogContext>();

export const logger = pino({
  level: "info",
  mixin() {
    return runLogContext.getStore() ?? {};
  },
});

export function configureLogger(level: PinoLogLevel): void {
  logger.level = level;
}

export function withRunLogContext<T>(runId: string, operation: () => Promise<T>): Promise<T> {
  return runLogContext.run({ runId }, operation);
}
