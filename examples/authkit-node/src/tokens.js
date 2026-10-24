// Signed session tokens (JWT, HS256), and short-lived links for unsubscribe emails.
import jwt from "jsonwebtoken";

const SECRET = process.env.AUTHKIT_SECRET ?? "dev-only-secret";

/** A signed token for `userId` that expires after `ttlMinutes`. */
export function issue(userId, ttlMinutes = 30) {
  return jwt.sign({ sub: userId }, SECRET, { algorithm: "HS256", expiresIn: `${ttlMinutes}m` });
}

/** The user ID in a valid token. Throws for bad, tampered or expired tokens. */
export function verify(token) {
  return jwt.verify(token, SECRET).sub;
}

/** The payload of a token without checking it, for logging which user a rejected token claimed to be. */
export function peek(token) {
  return jwt.decode(token)?.sub ?? null;
}
