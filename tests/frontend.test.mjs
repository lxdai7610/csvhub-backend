import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {SHA256} from '../csvhub/web/sha256.js';

for(const size of [0,1,3,55,56,63,64,65,127,128,129,4096,1000000]) {
  test(`streaming SHA256 matches native crypto, ${size} bytes`,()=>{
    const input=randomBytes(size),hash=new SHA256();
    for(let offset=0;offset<input.length;offset+=37)hash.update(input.subarray(offset,offset+37));
    assert.equal(hash.digest(),createHash('sha256').update(input).digest('hex'));
    assert.throws(()=>hash.update(input));
  });
}
test('known SHA256 vector, abc',()=>{
  assert.equal(new SHA256().update(new TextEncoder().encode('abc')).digest(),
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});
test('many updates stay bounded and digest matches native crypto',()=>{
  const part=randomBytes(65536),hash=new SHA256(),native=createHash('sha256');
  for(let i=0;i<256;i++){hash.update(part);native.update(part);}
  assert.equal(hash.buffer.byteLength,64);
  assert.equal(hash.digest(),native.digest('hex'));
});
