const fs = require('fs');

try {
    const src = '/cookies.txt';
    const dst = '/cookies.json';
    
    if (!fs.existsSync(src)) {
        console.log('[Cookie Converter] No cookies.txt found, skipping conversion.');
        process.exit(0);
    }

    const content = fs.readFileSync(src, 'utf8');
    const cookies = [];
    
    for (let line of content.split('\n')) {
        line = line.trim();
        if (!line || line.startsWith('#')) continue;
        
        const parts = line.split('\t');
        // Netscape format: domain, flag, path, secure, expiration, name, value
        if (parts.length >= 7 && parts[0].includes('.youtube.com')) {
            cookies.push(`${parts[5]}=${parts[6]}`);
        }
    }

    if (cookies.length > 0) {
        const json = { youtube: [cookies.join('; ')] };
        fs.writeFileSync(dst, JSON.stringify(json, null, 2));
        console.log(`[Cookie Converter] Successfully converted ${cookies.length} YouTube cookies from cookies.txt to cookies.json`);
    } else {
        console.log('[Cookie Converter] No YouTube cookies found in cookies.txt');
    }
} catch (err) {
    console.error('[Cookie Converter] Error converting cookies:', err);
}
