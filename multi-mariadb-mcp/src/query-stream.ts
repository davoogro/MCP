import type { FieldPacket, PoolConnection, RowDataPacket } from "mysql2/promise";
import { streamableConnection } from "./mysql-stream.js";

export interface StreamedQueryResult {
  rows: RowDataPacket[];
  columns: string[];
  truncated: boolean;
  connectionDestroyed: boolean;
}

// Collects rows from a streaming query, stopping after rowLimit rows (use maxRows + 1 to detect truncation).
export function streamQueryRows(
  client: PoolConnection,
  sql: string,
  rowLimit: number,
): Promise<StreamedQueryResult> {
  const coreConn = streamableConnection(client);
  return new Promise((resolve, reject) => {
    const rows: RowDataPacket[] = [];
    let columns: string[] = [];
    let truncated = false;
    let connectionDestroyed = false;
    let settled = false;

    const finish = (): void => {
      if (settled) {
        return;
      }
      settled = true;
      resolve({ rows, columns, truncated, connectionDestroyed });
    };

    const query = coreConn.query(sql);
    query.on("fields", (fields: FieldPacket[]) => {
      columns = fields.map((field) => field.name);
    });

    const stream = query.stream();
    stream.on("data", (row: RowDataPacket) => {
      if (truncated) {
        return;
      }
      if (rows.length < rowLimit) {
        rows.push(row);
        return;
      }
      truncated = true;
      stream.destroy();
      try {
        coreConn.destroy();
        connectionDestroyed = true;
      } catch {
        connectionDestroyed = true;
      }
      finish();
    });
    stream.on("error", (error: Error) => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    });
    stream.on("close", finish);
    stream.on("end", finish);
  });
}
