const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const pages = ['index.html', 'admin.html', 'quote.html'];
const documents = pages.map(file => fs.readFileSync(path.join(__dirname, file), 'utf8'));
const html = documents[0];
const scripts = documents.flatMap(document => [...document.matchAll(/<script(?:\s+nonce="[^"]+")?>([\s\S]*?)<\/script>/g)].map(match => match[1]));
assert.equal(scripts.length, pages.length, 'Each interactive page should have one inline script.');
scripts.forEach(script => assert.doesNotThrow(() => new Function(script), 'Inline JavaScript should parse.'));

const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const duplicateIds = ids.filter((id, index) => ids.indexOf(id) !== index);
assert.deepEqual(duplicateIds, [], 'Element IDs must be unique.');

const fragmentTargets = [...html.matchAll(/href="#([^"]+)"/g)].map(match => match[1]);
const missingTargets = fragmentTargets.filter(target => !ids.includes(target));
assert.deepEqual(missingTargets, [], 'Every in-page navigation link must have a target.');

const labels = [...html.matchAll(/<label\b[^>]*\bfor="([^"]+)"/g)].map(match => match[1]);
const missingLabelTargets = labels.filter(target => !ids.includes(target));
assert.deepEqual(missingLabelTargets, [], 'Every explicit form label must reference an element.');

assert.match(html, /@media\(prefers-reduced-motion:reduce\)/, 'Reduced-motion preferences must be respected.');
assert.match(html, /ZMW/, 'Public prices should use ZMW.');
assert.match(html, /\/api\/inquiries/, 'The consultation form must call its real backend endpoint.');
assert.match(html, /\/api\/pricing/, 'The estimator must load backend pricing.');
assert.match(html, /\/api\/samples/, 'The sample catalogue must load public sample data.');
assert.match(html, /This is not a confirmed engagement or quotation/, 'Inquiry success must not imply a confirmed engagement.');
assert.doesNotMatch(html, /type="file"/, 'The inquiry flow must not suggest files are uploaded when secure storage is not implemented.');
assert.match(scripts[0], /type\.value==='thesis'\|\|count>20/, 'Milestones must display for theses or projects above 20 pages.');
assert.match(scripts[0], /amount-first-second/, 'The final instalment must make rounded milestones sum to the total.');
const calculator = scripts[0].match(/function calculateZmw\([^}]+\}/);
const milestoneSplitter = scripts[0].match(/function splitMilestones\([^}]+\}/);
assert.ok(calculator, 'The ZMW calculator must expose a deterministic calculation function.');
assert.ok(milestoneSplitter, 'The milestone schedule must expose a deterministic split function.');
const calculateZmw = new Function(`${calculator[0]}; return calculateZmw;`)();
const splitMilestones = new Function(`${milestoneSplitter[0]}; return splitMilestones;`)();
const examplePricing = {rates_zmw:{assignment:150,proposal:220,thesis:280,editing:90},level_multipliers:{undergraduate:1,masters:1.25,phd:1.5},urgency_multipliers:{'24h':2.2,'3d':1.65,'7d':1.25,'14d':1}};
assert.equal(calculateZmw(examplePricing,'proposal','masters','14d',1),275,'Master’s proposal pricing should use the configured ZMW multipliers.');
assert.equal(calculateZmw(examplePricing,'proposal','masters','3d',10),4538,'Urgency and page count should be reflected before whole-kwacha rounding.');
assert.deepEqual(splitMilestones(4538),[1815,1361,1362],'Rounded installment amounts must add up exactly to the ZMW total.');
assert.deepEqual(splitMilestones(1),[0,0,1],'The final installment must retain any remainder for small totals.');
assert.match(scripts[0], /Idempotency-Key/, 'Inquiry submissions must include a duplicate-protection key.');
assert.match(scripts[1], /X-CSRF-Token/, 'Administrator mutations must include a CSRF token.');
assert.match(scripts[2], /\/api\/quotes\/accept/, 'Quotation responses must use the server endpoint.');

console.log(`Site checks passed: ${scripts.length} inline scripts parse; ${ids.length} unique public-page IDs; ${fragmentTargets.length} fragment links resolve; ${labels.length} public form labels resolve; backend integration safeguards are present.`);
