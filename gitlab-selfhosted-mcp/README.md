# GitLab MCP (self-hosted / SAML)

Servidor [MCP](https://modelcontextprotocol.io) para **GitLab self-hosted** usando [`@zereight/mcp-gitlab`](https://github.com/zereight/gitlab-selfhosted-mcp) — el paquete comunitario con **~217 tools** (MR, issues, pipelines, `push_files`, branches, wiki, etc.), mucho más completo que el MCP oficial de GitLab (~15 tools, requiere Premium/Ultimate).

Instancia objetivo: **https://gitlab.example.com/**

## SAML y autenticación: cómo funciona

**SAML solo aplica al login web** (navegador → IdP corporativo → GitLab).

El MCP **no habla SAML**. Llama a la **REST API** (`/api/v4`) y necesita credenciales de API:

| Método | Cuándo usarlo | SAML |
|--------|---------------|------|
| **Personal Access Token (PAT)** | Más simple, arrancar rápido | Te logueas con SAML en el navegador, luego creas el token en GitLab |
| **OAuth2 device flow** | Entornos SSO estrictos, sin PAT | Autorizas en el navegador donde ya tienes sesión SAML |
| **OAuth2 callback local** | Desktop con browser popup | Igual: el browser usa tu sesión SAML |

Si tu empresa **bloquea PATs**, usa OAuth (Opción B).

## Instalación

```powershell
cd C:\DVB\mcp\gitlab-selfhosted-mcp
copy .env.example .env
# Editar .env con tu método de auth (ver abajo)
```

Node.js **≥ 18** (Cursor trae uno embebido; el launcher lo usa vía `npx`).

## Opción A — Personal Access Token (recomendada para empezar)

1. Abre https://gitlab.example.com/ e inicia sesión con **SAML**.
2. **Avatar → Preferences → Access Tokens** (o *User Settings → Access Tokens*).
3. Crea un token con scope **`api`** (lectura + escritura vía API).
4. En `.env`:

```env
GITLAB_API_URL=https://gitlab.example.com/api/v4
GITLAB_PERSONAL_ACCESS_TOKEN=glpat-tu-token-aqui
GITLAB_PERMISSION_MODE=modify
```

`modify` permite crear/actualizar (incl. `push_files`) pero **bloquea deletes**. Usa `readonly` o `full` según necesites.

## Opción B — OAuth2 + SAML (device flow)

Ideal si PAT está restringido o prefieres revocar acceso desde GitLab UI.

### 1. Crear OAuth Application en GitLab

Tras login SAML:

1. **Preferences → Applications → Add new application**
2. **Name:** `Cursor GitLab MCP`
3. **Redirect URI:** `http://127.0.0.1:8888/callback` *(requerido al crear la app; device flow no abre localhost)*
4. **Confidential:** desmarcado (recomendado para desktop/CLI)
5. **Scopes:** `api`
6. Guarda el **Application ID** (`client_id`)

### 2. Configurar `.env`

```env
GITLAB_API_URL=https://gitlab.example.com/api/v4
GITLAB_USE_OAUTH=true
GITLAB_OAUTH_CLIENT_ID=tu-application-id
GITLAB_OAUTH_REDIRECT_URI=http://127.0.0.1:8888/callback
GITLAB_PERMISSION_MODE=modify
```

### 3. Autenticación one-time

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\DVB\mcp\gitlab-selfhosted-mcp\scripts\auth-oauth.ps1
```

Te muestra URL + código. Ábrelos en un navegador **donde ya estés logueado con SAML**, autoriza, y el token queda en `%USERPROFILE%\.gitlab-mcp-token.json`.

Requiere GitLab **17.9+** (device flow). En versiones antiguas, usa PAT.

## Cursor — registrar el MCP

Añade en `%USERPROFILE%\.cursor\mcp.json` (o copia `cursor-mcp.example.json`):

```json
"gitlab": {
  "command": "powershell.exe",
  "args": [
    "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "C:\\DVB\\mcp\\gitlab-selfhosted-mcp\\scripts\\launch.ps1"
  ],
  "envFile": "C:\\DVB\\mcp\\gitlab-selfhosted-mcp\\.env"
}
```

Reinicia Cursor o recarga MCP servers.

**Prueba:** pide al agente *"Lista mis proyectos en GitLab"*.

## Claude Code / clientes sin `cwd` ni `envFile`

`scripts/launch.mjs` carga `.env` relativo al propio script (las variables ya presentes en el
entorno tienen prioridad), resuelve rutas relativas (`GITLAB_CA_CERT_PATH`,
`NODE_EXTRA_CA_CERTS`, `GITLAB_OAUTH_TOKEN_PATH`) contra esta carpeta y arranca
`node_modules/@zereight/mcp-gitlab/build/index.js` en el mismo proceso. No escribe nada en stdout.

```powershell
claude mcp add gitlab-selfhosted --scope user -- "C:\Program Files\nodejs\node.exe" C:\DVB\mcp\gitlab-selfhosted-mcp\scripts\launch.mjs
```

## TLS corporativo

Si falla el certificado interno:

```env
GITLAB_CA_CERT_PATH=C:\DVB\mcp\gitlab-selfhosted-mcp\certs\gitlab-ca.pem
```

Exporta el CA corporativo a `certs/` (no commitear). Solo como último recurso:

```env
NODE_TLS_REJECT_UNAUTHORIZED=0
```

## Tools destacadas (vs MCP oficial)

- **Repos:** search, create, fork, tree, branches
- **Archivos:** read/create/update/delete, **`push_files`** (multi-file commit)
- **MR:** create, merge, rebase, approvals, diffs batched, discussions
- **CI/CD:** pipelines, jobs, logs, retry/cancel
- **Issues, wiki, milestones, releases, labels**
- **`discover_tools`** — activar más categorías en runtime

Filtrado: `GITLAB_TOOLSETS`, `GITLAB_TOOLS`, `GITLAB_DENIED_TOOLS_REGEX`.

## Seguridad

- **No** pongas tokens en `mcp.json` ni en git — solo en `.env` (gitignored).
- Revoca PAT/OAuth desde GitLab cuando dejes de usarlo.
- En repos sensibles considera `GITLAB_ALLOWED_PROJECT_IDS=123,456`.

## Referencias

- Repo: https://github.com/zereight/gitlab-selfhosted-mcp
- Docs OAuth: https://github.com/zereight/gitlab-selfhosted-mcp/blob/main/docs/auth/oauth-setup.md
- Cursor setup: https://github.com/zereight/gitlab-selfhosted-mcp/blob/main/docs/clients/cursor.md
