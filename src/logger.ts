import { AsyncLocalStorage } from "node:async_hooks";
import pino from "pino";
import { loadLoggingConfig } from "./config/schema";

interface LogContext {
  runId: string;
}

const logContext = new AsyncLocalStorage<LogContext>();
const loggingConfig = loadLoggingConfig();

export const logger = pino({
  level: loggingConfig.level,
  mixin() {
    return logContext.getStore() ?? {};
  },
  ...(loggingConfig.isProduction
    ? {}
    : {
        transport: {
          target: "pino-pretty",
          options: {
            colorize: true,
            translateTime: "HH:MM:ss",
            ignore: "pid,hostname",
          },
        },
      }),
});

export function withRunLogContext<T>(runId: string, operation: () => Promise<T>): Promise<T> {
  return logContext.run({ runId }, operation);
}
