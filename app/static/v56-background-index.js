(function () {
  const foregroundRequest = isApplicationRequest;
  isApplicationRequest = function (resource) {
    const value = typeof resource === 'string' ? resource : resource?.url || '';
    try {
      const path = new URL(value, window.location.href).pathname;
      if (path === '/api/v65/queue' || path === '/api/v82/movies/bulk-task-status' || path === '/api/v84/activity') return false;
      if (path === '/api/v39/setup/movie-index/status' || /^\/api\/(?:v54|v80)\/setup\/index\/[^/]+\/status$/.test(path)) return false;
    } catch (_) {}
    return foregroundRequest(resource);
  };
})();
