import { describe, it } from "vitest";

const enabled = process.env.RUN_WINMCP_INTEGRATION === "1";
const pilot = process.env.WINMCP_PILOT_SERVER_NAME;

describe.skipIf(!enabled || !pilot)("pilot server integration", () => {
  it("is configured for manual pilot validation only", () => {
    // Real integration requires pilot credentials, network access, and prepared remoting.
    // Run: npm run validate against config/servers.json with a real pilot entry.
  });
});
