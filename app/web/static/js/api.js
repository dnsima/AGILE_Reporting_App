/* Thin client for the AGILE JSON API.
 *
 * The dashboard calls exactly the same endpoints external consumers do, so the
 * UI can never drift from the documented API. The bearer token is kept in
 * sessionStorage for fetch calls; the server also sets an HttpOnly cookie,
 * which is what lets EventSource authenticate the live stream. */
window.AgileApi = (function () {
  "use strict";

  const PREFIX = (window.AGILE && window.AGILE.apiPrefix) || "/api/v1";
  const TOKEN_KEY = "agile.token";
  const USER_KEY = "agile.user";

  function readStore(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch (error) {
      return null; // private browsing or blocked storage
    }
  }

  function writeStore(key, value) {
    try {
      if (value === null) window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key, value);
    } catch (error) {
      /* non-fatal: the session cookie still authenticates the request */
    }
  }

  function token() {
    return readStore(TOKEN_KEY);
  }

  function currentUser() {
    const raw = readStore(USER_KEY);
    try {
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function setSession(payload) {
    writeStore(TOKEN_KEY, payload.access_token);
    writeStore(USER_KEY, JSON.stringify(payload.user));
  }

  function clearSession() {
    writeStore(TOKEN_KEY, null);
    writeStore(USER_KEY, null);
  }

  function headers(extra) {
    const value = Object.assign({}, extra || {});
    const jwt = token();
    if (jwt) value.Authorization = "Bearer " + jwt;
    return value;
  }

  async function handle(response) {
    if (response.status === 204) return null;
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch (error) {
      payload = { error: { message: text } };
    }
    if (!response.ok) {
      const detail = (payload && payload.error) || {};
      const error = new Error(detail.message || response.statusText || "Request failed");
      error.status = response.status;
      error.code = detail.code;
      error.details = detail.details;
      throw error;
    }
    return payload;
  }

  function query(params) {
    const search = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      const value = params[key];
      if (value === null || value === undefined || value === "") return;
      if (Array.isArray(value)) value.forEach((item) => search.append(key, item));
      else search.append(key, value);
    });
    const text = search.toString();
    return text ? "?" + text : "";
  }

  async function get(path, params) {
    const response = await fetch(PREFIX + path + query(params), {
      headers: headers({ Accept: "application/json" }),
      credentials: "same-origin",
    });
    return handle(response);
  }

  async function post(path, body, params) {
    const response = await fetch(PREFIX + path + query(params), {
      method: "POST",
      headers: headers({ "Content-Type": "application/json", Accept: "application/json" }),
      credentials: "same-origin",
      body: body === undefined ? null : JSON.stringify(body),
    });
    return handle(response);
  }

  async function upload(path, formData) {
    const response = await fetch(PREFIX + path, {
      method: "POST",
      headers: headers({ Accept: "application/json" }),
      credentials: "same-origin",
      body: formData,
    });
    return handle(response);
  }

  async function login(email, password) {
    const payload = await post("/auth/login", { email: email, password: password });
    setSession(payload);
    return payload;
  }

  async function logout() {
    try {
      await post("/auth/logout");
    } finally {
      clearSession();
    }
  }

  function downloadUrl(path, params) {
    return PREFIX + path + query(params);
  }

  /** Open the SSE stream; authentication travels on the session cookie. */
  function stream(handlers) {
    if (!window.EventSource) return null;
    const source = new EventSource(PREFIX + "/dashboard/stream", { withCredentials: true });
    source.addEventListener("hello", function () {
      if (handlers.onOpen) handlers.onOpen();
    });
    source.addEventListener("change", function (event) {
      if (!handlers.onChange) return;
      try {
        handlers.onChange(JSON.parse(event.data));
      } catch (error) {
        handlers.onChange(null);
      }
    });
    source.onerror = function () {
      if (handlers.onError) handlers.onError();
    };
    return source;
  }

  return {
    get: get,
    post: post,
    upload: upload,
    login: login,
    logout: logout,
    token: token,
    currentUser: currentUser,
    clearSession: clearSession,
    downloadUrl: downloadUrl,
    stream: stream,
  };
})();
