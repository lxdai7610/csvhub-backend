import {SHA256} from './sha256.js?v=0.3.7-vm';

export async function chunkDigest(chunk){
  if(globalThis.crypto?.subtle){
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',chunk)),x=>x.toString(16).padStart(2,'0')).join('');
  }
  return new SHA256().update(new Uint8Array(chunk)).digest();
}

export function requestIdentifier(){
  if(globalThis.crypto?.randomUUID)return crypto.randomUUID();
  const bytes=crypto.getRandomValues(new Uint8Array(16));
  bytes[6]=(bytes[6]&15)|64;
  bytes[8]=(bytes[8]&63)|128;
  const hex=Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
