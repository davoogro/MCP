import type { AppConfig, RemoteTransport, RemoteTransportFactory, ServerEntry } from "../types.js";
import { createPowerShellTransport } from "./powershell-remote.js";

export function createDefaultTransportFactory(config: AppConfig): RemoteTransportFactory {
  return async (_serverName: string, server: ServerEntry): Promise<RemoteTransport> => {
    return createPowerShellTransport(server, config.defaults);
  };
}

export { createPowerShellTransport };
