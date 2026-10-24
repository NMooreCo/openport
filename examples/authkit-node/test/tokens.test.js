import assert from "node:assert/strict";
import { test } from "node:test";
import jwt from "jsonwebtoken";

import { issue, peek, verify } from "../src/tokens.js";

test("a token round-trips to its user", () => {
  assert.equal(verify(issue("user-42")), "user-42");
});

test("an expired token is rejected", () => {
  const token = jwt.sign({ sub: "user-42", exp: Math.floor(Date.now() / 1000) - 60 }, "dev-only-secret");
  assert.throws(() => verify(token), /expired/);
});

test("a tampered token is rejected", () => {
  const [header, , signature] = issue("user-42").split(".");
  const forged = Buffer.from(JSON.stringify({ sub: "admin" })).toString("base64url");
  assert.throws(() => verify(`${header}.${forged}.${signature}`), /invalid signature/);
});

test("a token signed with another secret is rejected", () => {
  assert.throws(() => verify(jwt.sign({ sub: "user-42" }, "someone-else")), /invalid signature/);
});

test("peek reads the claimed user without verifying", () => {
  assert.equal(peek(jwt.sign({ sub: "user-7" }, "someone-else")), "user-7");
  assert.equal(peek("not-a-token"), null);
});
