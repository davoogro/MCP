import type { Connection } from "mysql2";
import type { PoolConnection } from "mysql2/promise";

// Underlying mysql2 connection (callback API) for query().stream().
export function streamableConnection(client: PoolConnection): Connection {
  return client.connection as unknown as Connection;
}
