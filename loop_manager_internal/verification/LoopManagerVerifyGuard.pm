package LoopManagerVerifyGuard;
use strict;
use warnings;
# Trusted tests must mock these Perl execution/network boundaries. CORE/XS bypasses are unsupported.
BEGIN {
    *CORE::GLOBAL::system = sub { die "verify: real process/Agent execution blocked\n" };
    *CORE::GLOBAL::exec = sub { die "verify: real process/Agent execution blocked\n" };
    *CORE::GLOBAL::readpipe = sub { die "verify: real process/Agent execution blocked\n" };
    *CORE::GLOBAL::fork = sub { die "verify: internal parallelism blocked\n" };
    *CORE::GLOBAL::socket = sub (*$$$) { die "verify: real network/Agent API blocked\n" };
    *CORE::GLOBAL::connect = sub (*$) { die "verify: real network/Agent API blocked\n" };
    *CORE::GLOBAL::open = sub (*;$@) {
        die "verify: pipe process/Agent execution blocked\n"
            if (@_ >= 3 && ($_[1] =~ /\|/) || @_ == 2 && $_[1] =~ /^\s*\||\|\s*$/);
        return CORE::open($_[0], $_[1]) if @_ == 2;
        return CORE::open($_[0], $_[1], $_[2]) if @_ == 3;
        die "verify: unsupported open form\n";
    };
}
1;
