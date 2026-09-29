import assert from "node:assert/strict";
import test from "node:test";
import pi from "../src/headroom_kit/resources/pi-extension.mjs";
import opencode from "../src/headroom_kit/resources/opencode-plugin/index.js";

test("Pi uses each API's base path without replacing credentials or models", () => {
  const previous = process.env.HEADROOM_KIT_ENDPOINT;
  const previousCopilot = process.env.HEADROOM_KIT_COPILOT_ENDPOINT;
  process.env.HEADROOM_KIT_ENDPOINT = "http://127.0.0.1:8790/v1";
  delete process.env.HEADROOM_KIT_COPILOT_ENDPOINT;
  try {
    const overrides = {};
    pi({ registerProvider: (id, config) => (overrides[id] = config) });
    assert.deepEqual(overrides, {
      openai: { baseUrl: "http://127.0.0.1:8790/v1" },
      "openai-codex": { baseUrl: "http://127.0.0.1:8790/v1" },
      anthropic: { baseUrl: "http://127.0.0.1:8790" },
    });
  } finally {
    if (previous === undefined) delete process.env.HEADROOM_KIT_ENDPOINT;
    else process.env.HEADROOM_KIT_ENDPOINT = previous;
    if (previousCopilot === undefined) delete process.env.HEADROOM_KIT_COPILOT_ENDPOINT;
    else process.env.HEADROOM_KIT_COPILOT_ENDPOINT = previousCopilot;
  }
});

test("Pi pins Copilot auth URLs without replacing native protocols, catalog, or refresh", async () => {
  const previous = process.env.HEADROOM_KIT_ENDPOINT;
  const previousCopilot = process.env.HEADROOM_KIT_COPILOT_ENDPOINT;
  process.env.HEADROOM_KIT_ENDPOINT = "http://127.0.0.1:8790/v1";
  process.env.HEADROOM_KIT_COPILOT_ENDPOINT = "http://127.0.0.1:8787/v1";
  try {
    const overrides = {};
    let startup;
    let registered;
    pi({
      registerProvider: (id, config) => {
        if (typeof id === "string") overrides[id] = config;
        else registered = id;
      },
      on: (event, handler) => {
        assert.equal(event, "session_start");
        startup = handler;
      },
    });
    const auth = { baseUrl: "http://127.0.0.1:8787", apiKey: "headroom-kit" };
    assert.deepEqual(overrides["github-copilot"], auth);
    assert.equal(overrides.openai.baseUrl, "http://127.0.0.1:8790/v1");
    const provider = {
      id: "github-copilot",
      getModels: () => [{ id: "future-model", api: "native-api" }],
      filterModels: (models) => models,
      stream: () => {},
      streamSimple: () => {},
      auth: {
        apiKey: { login: () => {}, resolve: () => assert.fail("native token used") },
        oauth: {
          login: () => {},
          refresh: () => {},
          toAuth: () => assert.fail("OAuth URL used"),
        },
      },
    };
    startup({}, {
      modelRegistry: {
        getProvider: (id) => {
          assert.equal(id, provider.id);
          return provider;
        },
      },
    });
    assert.deepEqual(registered, {
      ...provider,
      auth: {
        apiKey: { ...provider.auth.apiKey, resolve: registered.auth.apiKey.resolve },
        oauth: { ...provider.auth.oauth, toAuth: registered.auth.oauth.toAuth },
      },
    });
    assert.deepEqual(await registered.auth.apiKey.resolve(), { auth, source: "Headroom" });
    assert.deepEqual(await registered.auth.apiKey.resolve({ credential: { key: "fake-key" } }), {
      auth,
      source: "Headroom",
    });
    assert.deepEqual(await registered.auth.oauth.toAuth({ access: "fake-token" }), auth);
  } finally {
    if (previous === undefined) delete process.env.HEADROOM_KIT_ENDPOINT;
    else process.env.HEADROOM_KIT_ENDPOINT = previous;
    if (previousCopilot === undefined) delete process.env.HEADROOM_KIT_COPILOT_ENDPOINT;
    else process.env.HEADROOM_KIT_COPILOT_ENDPOINT = previousCopilot;
  }
});

for (const api of ["model", "catalog"]) {
  for (const copilot of [undefined, "http://127.0.0.1:8787/v1"]) {
    test(`OpenCode ${api} API routes native and SDK requests (Copilot: ${!!copilot})`, async (t) => {
      const endpoint = "http://127.0.0.1:8791/v1";
      const records = ["openai", "anthropic", "opencode", "github-copilot", "other"].map((id) => ({
        provider: { id },
        models: new Map([["model", { id: "model", providerID: id, websocket: true, package: "native-package" }]]),
      }));
      const entries = { list: () => records };
      const definitions = {
        update: (provider, model, edit) =>
          edit(records.find((item) => item.provider.id === provider).models.get(model)),
      };
      const hooks = {};
      let sdkHook;
      await opencode.setup({
        options: { endpoint, copilot },
        [api]: {
          transform: (change) => change(api === "model"
            ? { list: () => records.flatMap(({ models }) => [...models.values()]), ...definitions }
            : { provider: entries, model: definitions }),
        },
        session: { hook: (name, handler) => { hooks[name] = handler; } },
        aisdk: {
          hook: (name, handler) => {
            assert.equal(name, "sdk");
            sdkHook = handler;
          },
        },
      });
      t.mock.method(globalThis, "fetch", async (request) => request);
      for (const { provider, models } of records) {
        const isCopilot = provider.id === "github-copilot" && !!copilot;
        const supported = isCopilot || ["openai", "anthropic", "opencode"].includes(provider.id);
        assert.equal(models.get("model").websocket, !supported);
        assert.equal(models.get("model").package, "native-package");
        for (const kind of ["primary", "title", "compaction", "generate"]) {
          const event = {
            model: { providerID: provider.id },
            kind,
            baseURL: "https://other.example.invalid",
            headers: { authorization: "Bearer fake-test-key" },
            request: new Request("https://other.example.invalid/v1/messages", {
              headers: { Authorization: "Bearer fake-test-key", "x-api-key": "fake-key" },
            }),
          };
          hooks["model.request"](event);
          assert.equal(event.baseURL, isCopilot ? copilot : supported ? endpoint : "https://other.example.invalid");
          hooks["http.request"](event);
          assert.equal(event.request.headers.get("authorization"), isCopilot ? "Bearer headroom-kit" : "Bearer fake-test-key");
          assert.equal(event.request.headers.get("x-api-key"), isCopilot ? null : "fake-key");
        }
        const nativeFetch = () => assert.fail("native SDK fetch bypassed Headroom");
        const nativeSDK = {};
        const event = {
          model: { providerID: provider.id },
          options: { fetch: nativeFetch, endpoint: "responses" },
          sdk: nativeSDK,
        };
        sdkHook(event);
        assert.equal(event.sdk, nativeSDK);
        assert.equal(event.options.endpoint, "responses");
        if (!isCopilot) {
          assert.equal(event.options.fetch, nativeFetch);
          continue;
        }
        for (const path of ["/responses", "/v1/chat/completions?test=1"]) {
          const controller = new AbortController();
          const request = new Request(`https://other.example.invalid${path}`, {
            method: "POST",
            headers: {
              Authorization: "Bearer fake-key",
              "x-api-key": "fake-key",
              "X-Interaction-Type": "conversation-agent",
            },
            body: '{"model":"native-model"}',
            signal: controller.signal,
          });
          const routed = await event.options.fetch(request);
          assert.equal(routed.url, `http://127.0.0.1:8787${path}`);
          assert.equal(routed.method, "POST");
          assert.equal(routed.headers.get("authorization"), "Bearer headroom-kit");
          assert.equal(routed.headers.get("x-api-key"), null);
          assert.equal(routed.headers.get("x-interaction-type"), "conversation-agent");
          assert.equal(await routed.text(), '{"model":"native-model"}');
          controller.abort();
          assert.equal(routed.signal.aborted, true);
        }
      }
    });
  }
}
