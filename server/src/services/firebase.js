"use strict";

/**
 * Firebase Admin SDK init. Used to (a) verify the ID tokens the platform's
 * browser sends, and (b) read the caller's role from Firestore users/{uid}.
 *
 * Credentials resolve from GOOGLE_APPLICATION_CREDENTIALS (a service-account
 * JSON path). If auth is disabled for local dev, admin is never initialised.
 */
const admin = require("firebase-admin");
const config = require("../config");

let initialised = false;

function getAdmin() {
  if (config.authDisabled) return null;
  if (!initialised) {
    admin.initializeApp({
      credential: admin.credential.applicationDefault(),
      projectId: config.firebaseProjectId,
    });
    initialised = true;
  }
  return admin;
}

module.exports = { getAdmin };
