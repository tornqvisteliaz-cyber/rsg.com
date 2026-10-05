import { onRequest } from './cloudflare/functions/api/[[path]].js';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // API calls to Cloudflare Functions
    if (url.pathname.startsWith('/api/')) {
      return onRequest({
        request,
        env,
        params: { path: url.pathname.replace(/^\/api\/?/, '').split('/') },
        next: async () => new Response('Not found', { status: 404 }),
        data: {}
      });
    }

    // Clean URL rewrites for HTML pages
    const cleanRoutes = {
      '/': '/index.html',
      '/aircraft': '/aircraft.html',
      '/aircraft/seabee': '/aircraft/seabee.html',
      '/about': '/about.html',
      '/contact': '/contact.html',
      '/work-at-rsg': '/work-at-rsg.html',
      '/newsletter': '/newsletter.html',
      '/login': '/login.html',
      '/signup': '/signup.html',
      '/account': '/account.html',
      '/admin': '/admin.html',
      '/terms': '/terms.html',
      '/privacy': '/privacy.html'
    };

    if (cleanRoutes[url.pathname]) {
      const rewrittenUrl = new URL(request.url);
      rewrittenUrl.pathname = cleanRoutes[url.pathname];
      return env.ASSETS.fetch(new Request(rewrittenUrl, request));
    }

    return env.ASSETS.fetch(request);
  }
};