// Native routes use model.request; AI SDK routes need a fetch override.
export default {
  id: "headroom-kit",
  async setup(ctx) {
    const providers = new Set(["openai", "anthropic", "opencode"]);
    if (ctx.options.copilot) providers.add("github-copilot");
    // Edit final models, not provider templates that config can override later.
    await (ctx.model ?? ctx.catalog).transform((catalog) => {
      const models = ctx.model
        ? catalog.list()
        : catalog.provider.list().flatMap(({ models }) => [...models.values()]);
      const definitions = ctx.model ? catalog : catalog.model;
      for (const model of models) {
        if (!providers.has(model.providerID)) continue;
        definitions.update(model.providerID, model.id, (draft) => {
          draft.websocket = false;
        });
      }
    });
    // Copilot snapshots baseURL before this hook, but reads fetch when creating models.
    await ctx.aisdk.hook("sdk", (event) => {
      if (event.model.providerID !== "github-copilot" || !ctx.options.copilot) return;
      event.options.fetch = (input, init) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        const proxy = new URL(ctx.options.copilot);
        url.protocol = proxy.protocol;
        url.host = proxy.host;
        request.headers.delete("x-api-key");
        request.headers.set("authorization", "Bearer headroom-kit");
        return fetch(new Request(url, request));
      };
    });
    await ctx.session.hook("http.request", (event) => {
      if (event.model.providerID !== "github-copilot" || !ctx.options.copilot) return;
      event.request.headers.delete("x-api-key");
      event.request.headers.set("authorization", "Bearer headroom-kit");
    });
    await ctx.session.hook("model.request", (event) => {
      if (event.model.providerID === "github-copilot" && ctx.options.copilot) {
        event.baseURL = ctx.options.copilot;
        if (event.headers) event.headers.authorization = "Bearer headroom-kit";
        return;
      }
      if (providers.has(event.model.providerID)) event.baseURL = ctx.options.endpoint;
    });
  },
};
