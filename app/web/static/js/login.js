/* Sign-in page controller. */
(function () {
  "use strict";

  const form = document.getElementById("login-form");
  const error = document.getElementById("login-error");
  const button = document.getElementById("login-btn");

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    button.textContent = "Signing in…";

    try {
      await window.AgileApi.login(
        document.getElementById("email").value.trim(),
        document.getElementById("password").value
      );
      window.location.href = "/dashboard";
    } catch (exception) {
      error.textContent = exception.message || "Sign-in failed.";
      error.hidden = false;
      button.disabled = false;
      button.textContent = "Sign in";
    }
  });
})();
