export type D1Value = string | number | null;

export interface D1PreparedStatement {
  bind(...values: D1Value[]): D1PreparedStatement;
  first(): Promise<unknown>;
  all(): Promise<unknown>;
  run(): Promise<unknown>;
}

export interface D1DatabaseBinding {
  prepare(query: string): D1PreparedStatement;
  batch(statements: D1PreparedStatement[]): Promise<unknown>;
}
