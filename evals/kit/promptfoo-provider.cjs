// Optional runner adapter: no Promptfoo package dependency in the reference app.
const { spawnSync } = require('node:child_process');
const path = require('node:path');

module.exports = class LeakKitProvider {
  constructor(options = {}) { this.config = options.config || {}; }
  id() { return 'deterministic-retriever-leak-kit'; }
  async callApi() {
    const root = path.resolve(__dirname, '../..');
    const args = ['-m', 'evals.kit.run'];
    if (this.config.adapter) args.push('--adapter', path.resolve(root, this.config.adapter));
    if (this.config.fixture) args.push('--fixture', path.resolve(root, this.config.fixture));
    const result = spawnSync(this.config.python || 'python3', args,
      { cwd: root, encoding: 'utf8', timeout: 120000, maxBuffer: 1024 * 1024 });
    if (result.error || ![0, 1].includes(result.status)) {
      return { error: result.error?.message || result.stderr || 'kit process failed' };
    }
    try { return { output: JSON.parse(result.stdout) }; }
    catch (error) { return { error: 'invalid kit JSON: ' + error.message }; }
  }
};
