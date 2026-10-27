<?php
/*
 * Auto-poster login fix for WordPress (version 2)
 * ===============================================
 * Only needed when check_wp.py says "WordPress did not receive any login at all"
 * (rest_not_logged_in).
 *
 * It fixes the two usual causes:
 *   - the web server (often LiteSpeed) removes the "Authorization" header. The auto-poster
 *     also sends the login as "X-WP-Authorization", and this snippet hands it to WordPress.
 *   - another plugin asks "who is logged in?" too early, before WordPress knows the request
 *     is an API request, so WordPress never checks the Application Password. This snippet
 *     checks it again at the right moment.
 * WordPress still checks the Application Password with its own code. Your normal password
 * does not work through this, only Application Passwords.
 *
 * Every API answer gets an "X-Autopost-Login-Fix" header, so check_wp.py can tell
 * whether this snippet is running and whether the login reached it.
 *
 * How to add it (no cPanel or file access needed):
 *   1. WordPress dashboard > Plugins > Add New > search "Code Snippets" > Install > Activate.
 *   2. Snippets > Add New (or open your old "Auto-poster login fix" snippet and replace its code).
 *   3. Paste everything BELOW this comment (leave out the first line "<?php" and this comment).
 *   4. Choose "Run snippet everywhere" > "Save Changes and Activate".
 *
 * To undo it, deactivate or delete the snippet.
 */

// 1. Find the login: the usual places first, then the auto-poster's backup header.
$autopost_auth = '';
foreach ( array( 'HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION', 'HTTP_X_WP_AUTHORIZATION' ) as $autopost_key ) {
	if ( ! empty( $_SERVER[ $autopost_key ] ) ) {
		$autopost_auth = $_SERVER[ $autopost_key ];
		break;
	}
}
if ( '' === $autopost_auth && function_exists( 'apache_request_headers' ) ) {
	foreach ( (array) apache_request_headers() as $autopost_name => $autopost_value ) {
		if ( in_array( strtolower( $autopost_name ), array( 'authorization', 'x-wp-authorization' ), true ) ) {
			$autopost_auth = $autopost_value;
			break;
		}
	}
}
if ( empty( $_SERVER['PHP_AUTH_USER'] ) && 0 === stripos( $autopost_auth, 'Basic ' ) ) {
	$autopost_pair = base64_decode( substr( $autopost_auth, 6 ), true );
	if ( $autopost_pair && false !== strpos( $autopost_pair, ':' ) ) {
		list( $_SERVER['PHP_AUTH_USER'], $_SERVER['PHP_AUTH_PW'] ) = explode( ':', $autopost_pair, 2 );
	}
}

// 2. During API requests, check the Application Password if WordPress has not logged anyone in.
add_filter(
	'rest_authentication_errors',
	function ( $result ) {
		if ( ! empty( $result ) || is_user_logged_in() || empty( $_SERVER['PHP_AUTH_USER'] )
			|| ! function_exists( 'wp_authenticate_application_password' ) ) {
			return $result;
		}
		$password = isset( $_SERVER['PHP_AUTH_PW'] ) ? $_SERVER['PHP_AUTH_PW'] : '';
		$user     = wp_authenticate_application_password( null, $_SERVER['PHP_AUTH_USER'], $password );
		if ( $user instanceof WP_User ) {
			wp_set_current_user( $user->ID );
			return true;
		}
		if ( is_wp_error( $user ) ) {
			$data = (array) $user->get_error_data();
			if ( ! isset( $data['status'] ) ) {
				$data['status'] = 401;
				$user->add_data( $data );
			}
			return $user;
		}
		return $result;
	},
	5
);

// 3. Say that this snippet is running, and whether the login reached it.
add_filter(
	'rest_post_dispatch',
	function ( $response ) {
		if ( $response instanceof WP_REST_Response ) {
			$response->header(
				'X-Autopost-Login-Fix',
				empty( $_SERVER['PHP_AUTH_USER'] ) ? 'v2; login=missing' : 'v2; login=received'
			);
		}
		return $response;
	}
);
