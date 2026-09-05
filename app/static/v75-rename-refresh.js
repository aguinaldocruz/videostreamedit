(function () {
  const waitingForPlex=new Set(),wrappedApi=api;
  api=async function(resource,options){
    if(resource==='/api/v37/media/rename'){
      const result=await wrappedApi(resource,options);
      if(result.renamed&&result.path)waitingForPlex.add(result.path);
      return result;
    }
    if(resource==='/api/v39/movies/stream-filter-refresh'||resource==='/api/v38/movies/stream-filter-invalidate'||resource==='/api/v57/index/media-refresh'||resource==='/api/v54/index/invalidate'){
      const payload=JSON.parse(options?.body||'{}');
      if(payload.path&&waitingForPlex.delete(payload.path))return{indexed:false,queued:true,path:payload.path,waiting_for_plex:true};
    }
    return wrappedApi(resource,options);
  };
})();
