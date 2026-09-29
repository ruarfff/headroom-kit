// Loaded only by pi-headroom; Pi keeps ownership of credentials and model catalogs.
export default function (pi) {
  const endpoint = process.env.HEADROOM_KIT_ENDPOINT;
  for (const provider of ["openai", "openai-codex", "anthropic"]) {
    pi.registerProvider(provider, {
      baseUrl: provider === "anthropic" ? endpoint.replace(/\/v1$/, "") : endpoint,
    });
  }
  const copilot = process.env.HEADROOM_KIT_COPILOT_ENDPOINT;
  if (copilot) {
    const auth = { baseUrl: copilot.replace(/\/v1$/, ""), apiKey: "headroom-kit" };
    pi.registerProvider("github-copilot", auth);
    pi.on("session_start", (_event, ctx) => {
      const provider = ctx.modelRegistry.getProvider("github-copilot");
      // OAuth-derived URLs take precedence over model baseUrl overrides in Pi.
      pi.registerProvider({
        ...provider,
        auth: {
          apiKey: {
            ...provider.auth.apiKey,
            resolve: async () => ({ auth, source: "Headroom" }),
          },
          oauth: {
            ...provider.auth.oauth,
            toAuth: async () => auth,
          },
        },
      });
    });
  }
}
