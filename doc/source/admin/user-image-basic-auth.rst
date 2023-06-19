=================================================
HTTP(s) Basic Auth support for user image servers
=================================================

How to enable the feature via global configuration options
----------------------------------------------------------

There are 3 new variables that could be used to enable basic authentication
support. The 3 variables are structured such a way that 1 of them
`user_image_server_basic_auth` (boolean) enables (when `True`) and disables
(when `False`) basic authentication support.

The other two variables `user_image_server_password` and
`user_image_server_user` provide basic authentication username and password
credentials for user image download processes. `user_image_server_basic_auth`
not just enables the feature but enforces checks on the values of the 2 related
credentials.

When basic authentication support is enabled against the user image server an
exception will be raised in case any of the credentials are None or an empty
string. The variables belong to the `deploy` configuration group and could be
configured via the global Ironic configuration file.

Known limitations
-----------------

This implementation of the Basic Auth support for user image handling is
implemented via the global Ironic configuration process thus it doesn't
provide node specific customization options.

When `user_image_server_basic_auth` is set to `True` all user image sources
will be treated as Basic Auth enabled http servers and Ironic will
use the same single pair of credentials against all sources.

