import {cp,mkdir} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
for (const file of ['site.js','platform.js','app.js']) execFileSync(process.execPath,['--check',`web/${file}`],{stdio:'inherit'});
await mkdir('dist',{recursive:true});
await cp('web','dist',{recursive:true});
console.log('SUPIT website and illustrative financial workspace built in dist/.');
