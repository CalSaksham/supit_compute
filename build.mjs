import {cp,mkdir,readFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
for (const file of ['site.js','booking.js','scoring.js','providers.js','app.js']) execFileSync(process.execPath,['--check',`web/${file}`],{stdio:'inherit'});
JSON.parse(await readFile('web/providers.json','utf8'));
await mkdir('dist',{recursive:true});
await cp('web','dist',{recursive:true});
console.log('SUPIT website, provider finder and demo built in dist/.');
