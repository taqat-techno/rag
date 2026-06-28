import { AuthService } from "../auth";

export const API_VERSION = "v1";

interface Route {
  path: string;
  handler: () => void;
}

export class ApiRouter {
  private routes: Route[] = [];

  registerRoute(path: string, handler: () => void): void {
    this.routes.push({ path, handler });
  }

  createEndpoint(path: string): Route | undefined {
    return this.routes.find((r) => r.path === path);
  }
}

function bootstrap(): ApiRouter {
  return new ApiRouter();
}
