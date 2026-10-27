<?php
/*
 * Auto-poster login fix for WordPress
 * ===================================
 * Only needed when check_wp.py says "WordPress did not receive any login at all"
 * (rest_not_logged_in).
 *
 * Why: some web servers (often LiteSpeed) remove the "Authorization" header, so WordPress
 * never sees the Application Password. The auto-poster also sends the same login in an
 * "X-WP-Authorization" header, and this snippet hands it to WordPress. WordPress still
 * checks the Application Password exactly as usual - this only delivers it. Your normal
 * password does not work through it, only Application Passwords.
 *
 * How to add it (no cPanel or file access needed):
 *   1. WordPress dashboard > Plugins > Add New > search "Code Snippets" > Install > Activate.
 *   2. Snippets > Add New. Title: "Auto-poster login fix".
 *   3. Paste everything BELOW this comment (leave out the first line "<?php" and this comment).
 *   4. Choose "Run snippet everywhere" > "Save Changes and Activate".
 *
 * To undo it, deactivate or delete the snippet.
 */

if ( empty( $_SERVER['PHP_AUTH_USER'] ) ) {
	$autopost_auth = '';
	if ( ! empty( $_SERVER['HTTP_X_WP_AUTHORIZATION'] ) ) {
		$autopost_auth = $_SERVER['HTTP_X_WP_AUTHORIZATION'];
	} elseif ( function_exists( 'apache_request_headers' ) ) {
		// Some LiteSpeed servers still have the normal header here even when PHP did not get it.
		foreach ( (array) apache_request_headers() as $autopost_name => $autopost_value ) {
			if ( strtolower( $autopost_name ) === 'authorization' ) {
				$autopost_auth = $autopost_value;
				break;
			}
		}
	}
	if ( stripos( $autopost_auth, 'Basic ' ) === 0 ) {
		$autopost_pair = base64_decode( substr( $autopost_auth, 6 ), true );
		if ( $autopost_pair && strpos( $autopost_pair, ':' ) !== false ) {
			list( $_SERVER['PHP_AUTH_USER'], $_SERVER['PHP_AUTH_PW'] ) = explode( ':', $autopost_pair, 2 );
		}
	}
}
