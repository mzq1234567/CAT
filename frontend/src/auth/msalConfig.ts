import { Configuration, PopupRequest, AccountInfo, SilentRequest } from "@azure/msal-browser";

export const msalConfig: Configuration = {
  auth: {
    clientId: import.meta.env.VITE_AZURE_CLIENT_ID as string,
    // "common" supports work/school accounts across all tenants (multi-tenant)
    authority: "https://login.microsoftonline.com/common",
    redirectUri: window.location.origin,
    postLogoutRedirectUri: window.location.origin,
  },
  cache: {
    cacheLocation: "localStorage",
    storeAuthStateInCookie: false,
  },
};

export const loginRequest: PopupRequest = {
  scopes: ["openid", "profile", "email", "https://management.azure.com/user_impersonation"],
};

// ARM scope required to call Azure Resource Manager with delegated user identity
export const armScope = "https://management.azure.com/user_impersonation";

export function armTokenRequest(account: AccountInfo): SilentRequest {
  return { scopes: [armScope], account };
}
