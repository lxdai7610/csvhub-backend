import {SHA256} from './sha256.js';
self.onmessage = async ({data:{file}}) => {
  try {
    const hash=new SHA256(), chunk=2*1024*1024;
    for(let offset=0;offset<file.size;offset+=chunk) {
      const bytes=new Uint8Array(await file.slice(offset,offset+chunk).arrayBuffer());
      hash.update(bytes);
      self.postMessage({type:'progress',bytes:Math.min(offset+chunk,file.size)});
    }
    self.postMessage({type:'done',sha256:hash.digest()});
  } catch(error) { self.postMessage({type:'error',message:error.message}); }
};
