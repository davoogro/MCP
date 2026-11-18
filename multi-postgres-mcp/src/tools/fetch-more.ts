import { z } from "zod";
import { fetchMorePage } from "../query-cursors.js";
import { jsonError, jsonText } from "../security.js";
import { REMOTE_READ_ONLY } from "./annotations.js";

export const fetchMoreInputSchema = z.object({
  cursor: z.string().uuid().describe("Cursor id returned by execute_query when truncated=true"),
  maxRows: z
    .number()
    .int()
    .positive()
    .max(10_000)
    .optional()
    .describe("Rows to fetch in this page (cap 10000); defaults to the page size of the original query"),
});

export const fetchMoreTool = {
  name: "fetch_more",
  title: "Fetch next page from server cursor",
  description:
    "Continúa una consulta paginada cuyo cursor devolvió execute_query (DECLARE CURSOR en transacción READ ONLY). " +
    "Devuelve filas, truncated, cursor y nextOffset hasta agotar el resultado o expirar el cursor (TTL 10 min, máx 5 abiertos).",
  inputSchema: fetchMoreInputSchema,
  annotations: REMOTE_READ_ONLY,
  handler: () =>
    async ({ cursor, maxRows }: z.infer<typeof fetchMoreInputSchema>) => {
      try {
        const page = await fetchMorePage(cursor, maxRows);
        return jsonText({
          ...page,
          ...(page.truncated
            ? { note: "More rows remain; call fetch_more again with the same cursor." }
            : {}),
        });
      } catch (error) {
        return jsonError(error instanceof Error ? error.message : String(error));
      }
    },
};
